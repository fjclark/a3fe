"""Functions for running free energy calculations with SOMD with automated
 equilibration detection based on an ensemble of simulations."""

from enum import Enum as _Enum
import os as _os
import threading as _threading
import logging as _logging
from multiprocessing import Pool as _Pool
import numpy as _np
import pathlib as _pathlib
import pickle as _pkl
import subprocess as _subprocess
import scipy.stats as _stats
from time import sleep as _sleep
from typing import Dict as _Dict, List as _List, Tuple as _Tuple, Any as _Any, Optional as _Optional, Union as _Union

from ._virtual_queue import VirtualQueue as _VirtualQueue
from ..read._process_somd_files import write_simfile_option as _write_simfile_option
from .lambda_window import LamWindow as _LamWindow
from ..analyse.plot import (
    plot_gradient_stats as _plot_gradient_stats, 
    plot_gradient_hists as _plot_gradient_hists, 
    plot_gradient_timeseries as _plot_gradient_timeseries,
    plot_equilibration_time as _plot_equilibration_time,
    plot_overlap_mats as _plot_overlap_mats,
    plot_convergence as _plot_convergence,
    plot_mbar_pmf as _plot_mbar_pmf,
    plot_rmsds as _plot_rmsds
)
from ..analyse.mbar import run_mbar as _run_mbar
from ..analyse.process_grads import GradientData as _GradientData
from .enums import StageType as _StageType
from ..read._process_somd_files import write_simfile_option as _write_simfile_option
from ._simulation_runner import SimulationRunner as _SimulationRunner
from ._utils import get_simtime as _get_simtime



class Stage(_SimulationRunner):
    """
    Class to hold and manipulate an ensemble of SOMD simulations for a
    single stage of a calculation.
    """

    def __init__(self, 
                 stage_type: _StageType,
                 block_size: float = 1,
                 equil_detection: str = "block_gradient",
                 gradient_threshold: _Optional[float] = None,
                 runtime_constant: _Optional[float] = None,
                 ensemble_size: int = 5,
                 lambda_values: _Optional[_List[float]] = None,
                 base_dir: _Optional[str] = None,
                 input_dir: _Optional[str] = None,
                 output_dir: _Optional[str] = None,
                 stream_log_level: int = _logging.INFO) -> None:
        """
        Initialise an ensemble of SOMD simulations, constituting the Stage. If Stage.pkl exists in the
        output directory, the Stage will be loaded from this file and any arguments
        supplied will be overwritten.

        Parameters
        ----------
        stage_type : StageType
            The type of stage.
        block_size : float, Optional, default: 1
            Size of blocks to use for equilibration detection, in ns.
        equil_detection : str, Optional, default: "block_gradient"
            Method to use for equilibration detection. Options are:
            - "block_gradient": Use the gradient of the block averages to detect equilibration.
            - "chodera": Use Chodera's method to detect equilibration.
        gradient_threshold : float, Optional, default: None
            The threshold for the absolute value of the gradient, in kcal mol-1 ns-1,
            below which the simulation is considered equilibrated. If None, no theshold is
            set and the simulation is equilibrated when the gradient passes through 0. A 
            sensible value appears to be 0.5 kcal mol-1 ns-1.
        runtime_constant : float, Optional, default: None
            The runtime constant to use for the calculation. This must be supplied if running
            adaptively. Each window is run until the SEM**2 / runtime >= runtime_constant.
        ensemble_size : int, Optional, default: 5
            Number of simulations to run in the ensemble.
        lambda_values : List[float], Optional, default: None
            List of lambda values to use for the simulations. If None, the lambda values
            will be read from the simfile.
        base_dir : str, Optional, default: None
            Path to the base directory. If None,
            this is set to the current working directory.
        input_dir : str, Optional, default: None
            Path to directory containing input files for the simulations. If None, this
            will be set to "current_working_directory/input".
        output_dir : str, Optional, default: None
            Path to directory to store output files from the simulations. If None, this
            will be set to "current_working_directory/output".
        stream_log_level : int, Optional, default: logging.INFO
            Logging level to use for the steam file handlers for the
            Ensemble object and its child objects.

        Returns
        -------
        None
        """
        # Set the stage type first, as this is required for __str__,
        # and threrefore the super().__init__ call
        self.stage_type = stage_type

        super().__init__(base_dir=base_dir,
                         input_dir=input_dir,
                         output_dir=output_dir,
                         stream_log_level=stream_log_level,
                         ensemble_size=ensemble_size)

        if not self.loaded_from_pickle:
            if lambda_values is not None:
                self.lam_vals = lambda_values
            else:
                self.lam_vals = self._get_lam_vals()
            self.block_size = block_size
            self.equil_detection = equil_detection
            self.gradient_threshold = gradient_threshold
            self.runtime_constant = runtime_constant
            self._maximally_efficient = False # Set to True if the stage has been run so as to reach max efficieny
            self._running: bool = False
            self.run_thread: _Optional[_threading.Thread] = None
            # Set boolean to allow us to kill the thread
            self.kill_thread: bool = False
            self.running_wins: _List[_LamWindow] = []
            self.virtual_queue = _VirtualQueue(log_dir=self.base_dir)
            # Creating lambda window objects sets up required input directories
            lam_val_weights = self.lam_val_weights
            for i, lam_val in enumerate(self.lam_vals):
                lam_base_dir = _os.path.join(self.output_dir, f"lambda_{lam_val:.3f}")
                self.lam_windows.append(_LamWindow(lam=lam_val, 
                                                   lam_val_weight=lam_val_weights[i],
                                                   virtual_queue=self.virtual_queue,
                                                    block_size=self.block_size,
                                                    equil_detection=self.equil_detection,
                                                    gradient_threshold=self.gradient_threshold,
                                                    runtime_constant=self.runtime_constant,
                                                    ensemble_size=self.ensemble_size,
                                                    base_dir=lam_base_dir,
                                                    input_dir=self.input_dir,
                                                    stream_log_level=self.stream_log_level
                                                  )
                                       )

            # Point self._sub_sim_runners at the lambda windows
            self._sub_sim_runners = self.lam_windows

            # Save the state and update log
            self._update_log()
            self._dump()

    def __str__(self) -> str:
        return f"Stage (type = {self.stage_type.name.lower()})"

    @property
    def lam_windows(self) -> _List[_LamWindow]:
        return self._sub_sim_runners

    @lam_windows.setter
    def legs(self, value) -> None:
        self._logger.info("Modifying/ creating lambda windows")
        self._sub_sim_runners = value

    @property
    def lam_val_weights(self) -> _List[float]:
        """Return the weights for each lambda window. These are calculated
        according to how each windows contributes to the overall free energy
        estimate, as given by TI and the trapezoidal rule."""
        lam_val_weights = []
        for i, lam_val in enumerate(self.lam_vals):
            if i == 0:
                lam_val_weights.append(0.5 * (self.lam_vals[i+1] - lam_val))
            elif i == len(self.lam_vals) - 1:
                lam_val_weights.append(0.5 * (lam_val - self.lam_vals[i-1]))
            else:
                lam_val_weights.append(0.5 * (self.lam_vals[i+1] - self.lam_vals[i-1]))
        return lam_val_weights

    def run(self, adaptive:bool=True, runtime:_Optional[float]=None) -> None:
        """ Run the ensemble of simulations constituting the stage (optionally with adaptive 
        equilibration detection), and, if using adaptive equilibration detection, perform 
        analysis once finished. 

        Parameters
        ----------
        adaptive : bool, Optional, default: True
            If True, the stage will run until the simulations are equilibrated and perform analysis afterwards.
            If False, the stage will run for the specified runtime and analysis will not be performed.
        runtime : float, Optional, default: None
            If adaptive is False, runtime must be supplied and stage will run for this number of nanoseconds. 

        Returns
        -------
        None
        """
        if not adaptive and runtime is None:
            raise ValueError("If adaptive equilibration detection is disabled, a runtime must be supplied.")
        if adaptive and runtime is not None:
            raise ValueError("If adaptive equilibration detection is enabled, a runtime cannot be supplied.")

        # Run in the background with threading so that user can continuously check
        # the status of the Stage object
        self.run_thread = _threading.Thread(target=self._run_without_threading, args=(adaptive, runtime), name=str(self))
        self.run_thread.start()

    def kill(self) -> None:
        """Kill all running simulations."""
        # Stop the run loop
        if self.running:
            self._logger.info("Killing all lambda windows")
            self.kill_thread = True
            for win in self.lam_windows:
                win.kill()

    def _run_without_threading(self, 
                              adaptive:bool=True,
                              runtime:_Optional[float]=None,
                              max_runtime: float = 30) -> None:
        """ Run the ensemble of simulations constituting the stage (optionally with adaptive 
        equilibration detection), and, if using adaptive equilibration detection, perform 
        analysis once finished.  This function is called by run() with threading, so that
        the function can be run in the background and the user can continuously check 
        the status of the Stage object.
        
        Parameters
        ----------
        adaptive : bool, Optional, default: True
            If True, the stage will run until the simulations are equilibrated and perform analysis afterwards.
            If False, the stage will run for the specified runtime and analysis will not be performed.
        runtime : float, Optional, default: None
            If adaptive is False, runtime must be supplied and stage will run for this number of nanoseconds. 
        max_runtime : float, Optional, default: 30
            The maximum runtime for a single simulation during an adaptive simulation, in ns. Only used when adaptive == True. 

        Returns
        -------
        None
        """
        try:
            # Reset self.kill_thread so we can restart after killing
            self.kill_thread = False

            if not adaptive and runtime is None:
                raise ValueError("If adaptive equilibration detection is disabled, a runtime must be supplied.")
            if adaptive and runtime is not None:
                raise ValueError("If adaptive equilibration detection is enabled, a runtime cannot be supplied.")

            if not adaptive:
                self._logger.info(f"Starting {self}. Adaptive equilibration = {adaptive}...")
            elif adaptive:
                self._logger.info(f"Starting {self}. Adaptive equilibration = {adaptive}...")
                if runtime is None:
                    runtime = 0.2 # ns

            # Run initial SOMD simulations
            for win in self.lam_windows:
                win.run(runtime) # type: ignore
                win._update_log()
                self._dump()

            # Periodically check the simulations and analyse/ resubmit as necessary
            # Copy to ensure that we don't modify self.lam_windows when updating self.running_wins
            self.running_wins = self.lam_windows.copy() 
            self._dump()
            
            # Run the appropriate run loop
            if adaptive:
                self._run_loop_adaptive_efficiency()
            else: 
                self._run_loop_non_adaptive()

        except Exception as e:
            self._logger.exception("")
            raise e

        # All simulations are now finished, so perform final analysis
        self._logger.info(f"All simulations in {self} have finished.")

    def _run_loop_non_adaptive(self, cycle_pause: int = 60) -> None:
        """ The run loop for non-adaptive runs. Simply wait
        until all lambda windows have finished running.
        
        Parameters
        ----------
        cycle_pause : int, Optional, default: 60
            The number of seconds to wait between checking the status of the simulations."""

        while self.running_wins:
            _sleep(cycle_pause)  # Check every 60 seconds
            # Check if we've requested to kill the thread
            if self.kill_thread:
                self._logger.info(f"Kill thread requested: exiting run loop")
                return

            # Update the queue before checking the simulations
            self.virtual_queue.update()

            # Check if everything has finished
            for win in self.running_wins:
                # Check if the window has now finished - calling win.running updates the win._running attribute
                if not win.running:
                    self._logger.info(f"{win} has finished at {win.tot_simtime:.3f} ns")
                    self.running_wins.remove(win)

                    # Write status after checking for running and equilibration, as the
                    # _running and _equilibrated attributes have now been updated
                    win._update_log()
                    self._dump()

    def _run_loop_adaptive_equilibration(self, 
                                         cycle_pause: int = 60, # seconds
                                         max_runtime: float = 30 # ns
                                         ) -> None:
        """ Run loop which adaptively checks for equilibration, and resubmits
        the calculation if it has not equilibrated.
        
        Parameters
        ----------
        cycle_pause : int, Optional, default: 60
            The number of seconds to wait between checking the status of the simulations.
        max_runtime : float, Optional, default: 30
            The maximum runtime for a single simulation during an adaptive simulation, in ns."""

        while self.running_wins:
            _sleep(cycle_pause)  # Check every 60 seconds
            # Check if we've requested to kill the thread
            if self.kill_thread:
                self._logger.info(f"Kill thread requested: exiting run loop")
                return

            # Update the queue before checking the simulations
            self.virtual_queue.update()

            for win in self.running_wins:
                # Check if the window has now finished - calling win.running updates the win._running attribute
                if not win.running:
                    # If we are in adaptive mode, check if the simulation has equilibrated and if not, resubmit
                    if win.equilibrated:
                        self._logger.info(f"{win} has equilibrated at {win.equil_time:.3f} ns")
                        self.running_wins.remove(win)
                    else:
                        # Check that we haven't exceeded the maximum runtime for any simulations
                        if win.tot_simtime / win.ensemble_size >= max_runtime:
                            self._logger.info(f"{win} has not equilibrated but simulations have exceeded the maximum runtime of "
                                                f"{max_runtime} ns. Terminating simulations") 
                            self.running_wins.remove(win)
                        else: # Not equilibrated and not over the maximum runtime, so resubmit
                            self._logger.info(f"{win} has not equilibrated. Resubmitting for {self.block_size:.3f} ns")
                            win.run(self.block_size)

                    # Write status after checking for running and equilibration, as the
                    # _running and _equilibrated attributes have now been updated
                    win._update_log()
                    self._dump()

        
    def _run_loop_adaptive_efficiency(self, 
                                        cycle_pause: int = 60, # seconds
                                        maximum_runtime: float = 30 # ns
                                        ) -> None:
        """ Run loop which allocates sampling time in order to achieve maximal estimation
        efficiency of the free energy difference.
        
        Parameters
        ----------
        cycle_pause : int, Optional, default: 60
            The number of seconds to wait between checking the status of the simulations.
        maximum_runtime : float, Optional, default: 30
            The maximum runtime for a single simulation during an adaptive simulation, in ns."""

        while not self._maximally_efficient:

            # Firstly, wait til all window have finished
            while self.running_wins:
                _sleep(cycle_pause)  # Check every 60 seconds
                # Check if we've requested to kill the thread
                if self.kill_thread:
                    self._logger.info(f"Kill thread requested: exiting run loop")
                    return
                # Update the queue before checking the simulations
                self.virtual_queue.update()
                for win in self.running_wins:
                    if not win.running:
                        self.running_wins.remove(win)
                    # Write status after checking for running and equilibration, as the
                    # _running and _equilibrated attributes have now been updated
                    win._update_log()
                    self._dump()

            # Now all windows have finished, check if we have reached the maximum 
            # efficiency and resubmit if not
            # Get the gradient data and extract the per-lambda SEM of the free energy change
            gradient_data = _GradientData(lam_winds=self.lam_windows, equilibrated=False)
            smooth_dg_sems = gradient_data.get_sems(origin="inter_delta_g", smoothen=True)

            # For each window, calculate the predicted most efficient run time. See if we have reached this
            # and if not, resubmit for this remaining time
            for i, win in enumerate(self.lam_windows):
                normalised_sem_dg = smooth_dg_sems[i]
                predicted_run_time_max_eff = (1 / _np.sqrt(self.runtime_constant)) * normalised_sem_dg
                win._logger.info(f"Predicted maximum efficiency run time for is {predicted_run_time_max_eff:.3f} ns")
                # Make sure that we don't exceed the maximum per-simulation runtime
                if predicted_run_time_max_eff > maximum_runtime * win.ensemble_size:
                    win._logger.info(f"Predicted maximum efficiency run time per window is "
                                        f"{predicted_run_time_max_eff / win.ensemble_size}, which exceeds the maximum runtime of "
                                        f"{maximum_runtime} ns. Running to the maximum runtime instead.")
                    predicted_run_time_max_eff = maximum_runtime * win.ensemble_size
                actual_run_time = win.tot_simtime
                if actual_run_time < predicted_run_time_max_eff - 0.1: # Small tolerance to avoid rounding errors
                    resubmit_time = (predicted_run_time_max_eff - actual_run_time) / win.ensemble_size
                    resubmit_time = round(resubmit_time, 1) # Round to the nearest 0.1 ns
                    # We have not reached the maximum efficiency, so resubmit for the remaining time
                    if resubmit_time > 0:
                        win._logger.info(f"Window has not reached maximum efficiency. Resubmitting for {resubmit_time:.3f} ns")
                        win.run(resubmit_time)
                        self.running_wins.append(win)
                else: # We have reached or exceeded the maximum efficiency runtime
                    win._logger.info(f"Window has reached the most efficient run time at {actual_run_time}. "
                                        "No further simulation required")

            # If there are no running lambda windows, we must have reached the maximum efficiency
            if not self.running_wins:
                self._maximally_efficient = True
            
    def get_optimal_lam_vals(self, 
                             er_type: str = "sem",
                             delta_er: _Optional[float] = None, 
                             n_lam_vals: _Optional[int] = None,
                             ) -> _np.ndarray:
        """
        Get the optimal lambda values for the stage, based on the 
        integrated SEM, and create plots.

        Parameters
        ----------
        er_type: str, optional, default="sem"
            Whether to integrate the standard error of the mean ("sem") or root 
            variance of the gradients ("root_var") to calculate the optimal 
            lambda values.
        delta_er : float, optional, default=None
            If er_type == "root_var", the desired integrated root variance of the gradients
            between each lambda value, in kcal mol^(-1). If er_type == "sem", the
            desired integrated standard error of the mean of the gradients between each lambda
            value, in kcal mol^(-1) ns^(1/2). A sensible default for sem is 0.1 kcal mol-1 ns1/2,
            and for root_var is 1 kcal mol-1.  If not provided, the number of lambda windows must be 
            provided with n_lam_vals.    
        n_lam_vals : int, optional, default=None
            The number of lambda values to sample. If not provided, delta_er must be provided.
        Returns
        -------
        optimal_lam_vals : np.ndarray
            List of optimal lambda values for the stage.
        """
        self._logger.info(f"Calculating optimal lambda values with er_type = {er_type} and delta_er = {delta_er}...")
        unequilibrated_gradient_data = _GradientData(lam_winds=self.lam_windows, equilibrated=False)
        for plot_type in ["mean", "intra_run_variance", "sem", "stat_ineff", "integrated_sem", "integrated_var"]:
            _plot_gradient_stats(gradients_data=unequilibrated_gradient_data, output_dir=self.output_dir, plot_type=plot_type)
        _plot_gradient_hists(gradients_data=unequilibrated_gradient_data, output_dir=self.output_dir)
        return unequilibrated_gradient_data.calculate_optimal_lam_vals(er_type=er_type, 
                                                                       delta_er=delta_er,
                                                                       n_lam_vals=n_lam_vals)


    def _get_lam_vals(self) -> _List[float]:
        """
        Return list of lambda values for the simulations,
        based on the configuration file.

        Returns
        -------
        lam_vals : List[float]
            List of lambda values for the simulations.
        """
        # Read number of lambda windows from input file
        lam_vals_str = []
        with open(self.input_dir + "/somd.cfg", "r") as ifile:
            lines = ifile.readlines()
            for line in lines:
                if line.startswith("lambda array ="):
                    lam_vals_str = line.split("=")[1].split(",")
                    break
        lam_vals = [float(lam) for lam in lam_vals_str]

        return lam_vals

    def analyse(self, get_frnrg:bool = True, subsampling=False) -> _Union[_Tuple[float, float], _Tuple[None, None]]:
        r""" Analyse the results of the ensemble of simulations. Requires that
        all lambda windows have equilibrated.
          
        Parameters
        ----------
        get_frnrg : bool, optional, default=True
            If True, the free energy will be calculated with MBAR, otherwise
            this will be skipped.
        subsampling: bool, optional, default=False
            If True, the free energy will be calculated by subsampling using
            the methods contained within pymbar.
        
        Returns
        -------
        free_energies : np.ndarray or None
            The free energy changes for the stage for each of the ensemble
            size runs, in kcal mol-1.  If get_frnrg is False, this is None.
        errors : np.ndarray or None
            The MBAR error estimates for the free energy changes for the stage
            for each of the ensemble size runs, in kcal mol-1.  If get_frnrg is
            False, this is None.
        """
        # Check that this is not still running
        if self.running:
            raise RuntimeError(f"Cannot perform analysis as the Stage is still running")

        # Check that none of the simulations have failed
        failed_sims_list = self.failed_simulations
        if failed_sims_list:
            self._logger.error("Unable to perform analysis as several simulations did not complete successfully")
            self._logger.error("Please check the output in the following directories:")
            for failed_sim in failed_sims_list:
                self._logger.error(failed_sim.base_dir)
            raise RuntimeError("Unable to perform analysis as several simulations did not complete successfully")

        # Check that all simulations have equilibrated
        for win in self.lam_windows:
            # Avoid checking win.equilibrated as this causes expensive equilibration detection to be run
            if not win._equilibrated:
                raise RuntimeError("Not all lambda windows have equilibrated. Analysis cannot be performed.")
            if win.equil_time is None:
                raise RuntimeError("Despite equilibration being detected, no equilibration time was found.")

        if get_frnrg:
            self._logger.info("Computing free energy changes using the MBAR")

            # Remove unequilibrated data from the equilibrated output directory
            for win in self.lam_windows:
                equil_time = win.equil_time
                # Minus 1 because first energy is only written after the first nrg_freq steps
                equil_index = int(equil_time / (win.sims[0].timestep * win.sims[0].nrg_freq)) - 1
                for sim in win.sims:
                    in_simfile = sim.output_dir + "/simfile.dat"
                    out_simfile = sim.output_dir + "/simfile_equilibrated.dat"
                    self._write_equilibrated_data(in_simfile, out_simfile, equil_index)

            
            # Run MBAR and compute mean and 95 % C.I. of free energy
            free_energies, errors, mbar_outfiles = _run_mbar(output_dir=self.output_dir, 
                                                             ensemble_size=self.ensemble_size,
                                                             percentage=100,
                                                             subsampling=subsampling)
            mean_free_energy = _np.mean(free_energies)
            # Gaussian 95 % C.I.
            conf_int = _stats.t.interval(0.95,
                                        len(free_energies)-1,
                                        mean_free_energy,
                                        scale=_stats.sem(free_energies))[1] - mean_free_energy  # 95 % C.I.

            # Write overall MBAR stats to file
            with open(f"{self.output_dir}/overall_stats.dat", "a") as ofile:
                if get_frnrg:
                    ofile.write("###################################### Free Energies ########################################\n")
                    ofile.write(f"Mean free energy: {mean_free_energy: .3f} + /- {conf_int:.3f} kcal/mol\n")
                    for i in range(self.ensemble_size):
                        ofile.write(f"Free energy from run {i+1}: {free_energies[i]: .3f} +/- {errors[i]:.3f} kcal/mol\n")
                    ofile.write("Errors are 95 % C.I.s based on the assumption of a Gaussian distribution of free energies\n")

            # Plot overlap matrices and PMFs
            _plot_overlap_mats(output_dir=self.output_dir, mbar_outfiles=mbar_outfiles)
            _plot_mbar_pmf(mbar_outfiles, self.output_dir)
            equilibrated_gradient_data = _GradientData(lam_winds=self.lam_windows, equilibrated=True)
            _plot_overlap_mats(output_dir=self.output_dir, predicted=True, gradient_data=equilibrated_gradient_data)

        # Plot RMSDS
        self._logger.info("Plotting RMSDs")
        selections = ["resname LIG and (not name H*)"] #, "protein"]
        #for selection in selections:
            #_plot_rmsds(lam_windows=self.lam_windows, output_dir=self.output_dir, selection=selection)

        # Analyse the gradient data and make plots
        self._logger.info("Plotting gradients data")
        equilibrated_gradient_data = _GradientData(lam_winds=self.lam_windows, equilibrated=True)
        for plot_type in ["mean",
                          "intra_run_variance",
                          "sem",
                          "stat_ineff",
                          "integrated_sem",
                          "integrated_var",
                          "sq_sem_sim_time",
                          "pred_best_simtime",]:
            _plot_gradient_stats(gradients_data=equilibrated_gradient_data, output_dir=self.output_dir, plot_type=plot_type)
        _plot_gradient_hists(gradients_data=equilibrated_gradient_data, output_dir=self.output_dir)
        _plot_gradient_timeseries(gradients_data=equilibrated_gradient_data, output_dir=self.output_dir)

        # Make plots of equilibration time
        self._logger.info("Plotting equilibration times")
        _plot_equilibration_time(lam_windows=self.lam_windows, output_dir=self.output_dir)


        # Write out stats
        with open(f"{self.output_dir}/overall_stats.dat", "a") as ofile:
            for win in self.lam_windows:
                ofile.write(f"Equilibration time for lambda = {win.lam}: {win.equil_time:.3f} ns per simulation\n")
                ofile.write(f"Total time simulated for lambda = {win.lam}: {win.sims[0].tot_simtime:.3f} ns per simulation\n")

        if get_frnrg:
            self._logger.info(f"Overall free energy changes: {free_energies} kcal mol-1") # type: ignore
            self._logger.info(f"Overall errors: {errors} kcal mol-1") # type: ignore
            # Update the interally-stored results
            self._delta_g = free_energies
            self._delta_g_er = errors
            return free_energies, errors # type: ignore
        else:
            return None, None

    def analyse_convergence(self) -> _Tuple[_np.ndarray, _np.ndarray]:
        """
        Get a timeseries of the total free energy change of the
        stage against total simulation time. Also plot this.
        This is kept separate from the analyse method as it is
        expensive to run.
        
        Returns
        -------
        fracts : np.ndarray
            The fraction of the total equilibrated simulation time for each value of dg_overall.
        dg_overall : np.ndarray
            The overall free energy change for the stage for each value of total equilibrated 
            simtime for each of the ensemble size repeats. 
        """
        self._logger.info("Analysing convergence...")
        
        # Get the dg_overall in terms of fraction of the total simulation time
        # Use steps of 5 % of the total simulation time
        fracts = _np.arange(0.05, 1.05, 0.05)
        percents = fracts * 100
        dg_overall = _np.zeros(len(fracts))

        # Now run mbar with multiprocessing to speed things up
        with _Pool() as pool:
            results = pool.starmap(_run_mbar, [(self.output_dir, self.ensemble_size, percent, False, 298, True) for percent in percents])
            dg_overall = _np.array([result[0] for result in results]).transpose() # result[0] is a 2D array for a given percent

        self._logger.info(f"Overall free energy changes: {dg_overall} kcal mol-1")
        self._logger.info(f"Fractions of equilibrated simulation time: {fracts}")

        # Plot the overall convergence
        _plot_convergence(fracts, dg_overall, self.tot_simtime, self.equil_time, self.output_dir, self.ensemble_size)

        return fracts, dg_overall

    def _write_equilibrated_data(self, in_simfile: str,
                                 out_simfile: str,
                                 equil_index: int) -> None:
        """
        Remove unequilibrated data from a simulation file and write a new
        file containing only the equilibrated data.

        Parameters
        ----------
        in_simfile : str
            Path to simulation file.
        out_simfile : str
            Path to new simulation file, containing only the equilibrated data
            from the original file.
        equil_index : int
            Index of the first equilibrated frame, given by
            equil_time / (timestep * nrg_freq)

        Returns
        -------
        None
        """
        with open(in_simfile, "r") as ifile:
            lines=ifile.readlines()

        # Figure out how many lines come before the data
        non_data_lines=0
        for line in lines:
            if line.startswith("#"):
                non_data_lines += 1
            else:
                break

        # Overwrite the original file with one containing only the equilibrated data
        with open(out_simfile, "w") as ofile:
            # First, write the header
            for line in lines[:non_data_lines]:
                ofile.write(line)
            # Now write the data, skipping the non-equilibrated portion
            for line in lines[equil_index + non_data_lines:]:
                ofile.write(line)

    def setup(self) -> None:
        raise NotImplementedError("Stages are set up when they are created")

    @property
    def tot_simtime(self) -> float:
        f"""The total simulation time in ns for the stage."""
        # Use multiprocessing at the level of stages to speed this us - this is a good place as stages
        # have lots of windows, so we benefit the most from parallelisation here.
        with _Pool() as pool:
            return sum(pool.map(_get_simtime, self._sub_sim_runners))

    def _mv_output(self, save_name: str) -> None:
        """
        Move the output directory to a new location, without
        changing self.output_dir.

        Parameters
        ----------
        save_name : str
            The new name of the old output directory.
        """
        self._logger.info(f"Moving contents of output directory to {save_name}")
        base_dir = _pathlib.Path(self.output_dir).parent.resolve()
        _os.rename(self.output_dir, _os.path.join(base_dir, save_name))

    def set_simfile_option(self, option: str, value: str) -> None:
        """Set the value of an option in the simulation configuration file."""
        simfile = _os.path.join(self.input_dir, "somd.cfg")
        _write_simfile_option(simfile, option, value)
        super().set_simfile_option(option, value)

    def update(self, save_name: str = "output_saved") -> None:
        """
        Delete the current set of lamda windows and simulations, and
        create a new set of simulations based on the current state of
        the stage. This is useful if you want to change the number of
        simulations per lambda window, or the number of lambda windows.

        Parameters
        ----------
        save_name : str, default "output_saved"
            The name of the directory to save the old output directory to.
        """
        if self.running:
            raise RuntimeError("Can't update while ensemble is running")
        if _os.path.isdir(self.output_dir):
            self._mv_output(save_name)
        # Update the list of lambda windows in the simfile
        _write_simfile_option(simfile=f"{self.input_dir}/somd.cfg",
                              option="lambda array", 
                              value=", ".join([str(lam) for lam in self.lam_vals]))
        self._logger.info("Deleting old lambda windows and creating new ones...")
        self._sub_sim_runners = []
        lam_val_weights = self.lam_val_weights
        for i, lam_val in enumerate(self.lam_vals):
            lam_base_dir = _os.path.join(self.output_dir, f"lambda_{lam_val:.3f}")
            self.lam_windows.append(_LamWindow(lam=lam_val, 
                                                lam_val_weight = lam_val_weights[i],
                                                virtual_queue=self.virtual_queue,
                                                block_size=self.block_size,
                                                equil_detection=self.equil_detection,
                                                gradient_threshold=self.gradient_threshold,
                                                runtime_constant=self.runtime_constant,
                                                ensemble_size=self.ensemble_size,
                                                base_dir=lam_base_dir,
                                                input_dir=self.input_dir,
                                                stream_log_level=self.stream_log_level
                                                )
                                    )

class StageContextManager():
    """Stage context manager to ensure that all stages are killed when
    the context is exited."""

    def __init__(self, stages: _List[Stage]):
        """ 
        Parameters
        ----------
        stages : List[Stage]
            The stages to kill when the context is exited.
        """
        self.stages = stages

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for stage in self.stages:
            stage.kill()
        