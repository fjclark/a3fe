"""Functionality to process the gradient data."""

import numpy as _np
from typing import List as _List, Tuple as _Tuple, Optional as _Optional, Dict as _Dict, Union as _Union
from scipy.constants import gas_constant as _R

from .autocorrelation import get_statistical_inefficiency as _get_statistical_inefficiency

class GradientData():
    """A class to store and process gradient data."""

    def __init__(self, lam_winds: _List["LamWindow"], equilibrated: bool, )-> None: # type: ignore
        """ 
        Calculate the gradients, means, and variances of the gradients for each lambda
        window of a list of LamWindows.

        Parameters
        ----------
        lam_winds : List[LamWindow]
            List of lambda windows.
        equilibrated : bool
            If True, only equilibrated data is used.
        """
        self.equilibrated = equilibrated

        # Get mean and variance of gradients, including both intra-run and inter-run components
        lam_vals = []
        gradients_all_winds = []
        gradients_subsampled_all_winds = []
        means_all_winds = []
        sems_tot_all_winds = []
        sems_intra_all_winds = []
        sems_inter_all_winds = []
        vars_intra_all_winds = []
        stat_ineffs_all_winds = []

        for lam in lam_winds:
            # Record the lambda value
            lam_vals.append(lam.lam)

            # Get all gradients and statistical inefficiencies
            gradients_wind = []
            means_intra = []
            stat_ineffs_wind = []
            gradients_subsampled_wind = []
            vars_intra = []
            squared_sems_intra = []
            # Get intra-run quantities
            for sim in lam.sims:
                # Get the gradients, mean, and statistical inefficiencies
                _, gradients = sim.read_gradients(equilibrated_only=equilibrated)
                stat_ineff = _get_statistical_inefficiency(gradients)
                mean = _np.mean(gradients)
                # Subsample the gradients to remove autocorrelation
                subsampled_grads = gradients[::int(stat_ineff)]
                # Get the variance and squared SEM of the gradients
                var = _np.var(subsampled_grads)
                squared_sem = var / len(subsampled_grads)
                # Store the results
                gradients_wind.append(gradients)
                means_intra.append(mean)
                stat_ineffs_wind.append(stat_ineff)
                gradients_subsampled_wind.append(subsampled_grads)
                vars_intra.append(var)
                squared_sems_intra.append(squared_sem)

            # Get overall intra-run quantities
            var_intra = _np.mean(vars_intra)
            squared_sem_intra = _np.mean(squared_sems_intra) / len(lam.sims) 
            stat_ineff = _np.mean(stat_ineffs_wind)

            # Get inter-run quantities
            squared_sem_inter = _np.var(means_intra) / len(lam.sims)
            mean_overall = _np.mean(means_intra)

            # Store the final results, converting to arrays for consistency.
            tot_sem = _np.sqrt(squared_sem_inter + squared_sem_intra)  # This isn't really a meaningful quantity
            sem_intra = _np.sqrt(squared_sem_intra)
            sem_inter = _np.sqrt(squared_sem_inter)
            gradients_all_winds.append(_np.array(gradients_wind))
            gradients_subsampled_all_winds.append(gradients_subsampled_wind)
            means_all_winds.append(mean_overall)
            sems_tot_all_winds.append(tot_sem)
            sems_intra_all_winds.append(sem_intra)
            sems_inter_all_winds.append(sem_inter)
            vars_intra_all_winds.append(var_intra)
            stat_ineffs_all_winds.append(stat_ineff)

        # Get the statistical inefficiencies in units of simulation time
        stat_ineffs_all_winds = _np.array(stat_ineffs_all_winds) * lam_winds[0].sims[0].timestep # Timestep should be same for all sims

        # Get the times
        if equilibrated:
            start_times = _np.array([win._equil_time for win in lam_winds])
        else:
            start_times = _np.array([0 for win in lam_winds])
        end_times = _np.array([win.sims[0].tot_simtime for win in lam_winds]) # All sims at given lam run for same time
        times = [_np.linspace(start, end, len(gradients[0]) + 1)[1:] for start, end, gradients in zip(start_times, end_times, gradients_all_winds)]

        # Get the total sampling time per window
        sampling_times = end_times - start_times

        # Save the calculated attributes
        self.n_lam = len(lam_vals)
        self.lam_vals = lam_vals
        self.gradients = gradients_all_winds
        self.subsampled_gradients = gradients_subsampled_all_winds
        self.times = times
        self.sampling_times = sampling_times
        self.means = means_all_winds
        self.sems_overall = sems_tot_all_winds
        self.sems_intra = sems_intra_all_winds
        self.sems_inter = sems_inter_all_winds
        self.vars_intra = vars_intra_all_winds
        self.stat_ineffs = stat_ineffs_all_winds

    def get_sems(self,
                 origin: str = "inter",
                 smoothen: bool = True)-> _np.ndarray:
        """
        Return the standardised standard error of the mean of the gradients, optionally
        smoothened by a block average over 3 points.
        
        Parameters
        ----------
        origin: str, optional, default="inter"
            Whether to use the inter-run or intra-run standard error of the mean
        smoothen: bool, optional, default=True
            Whether to smoothen the standard error of the mean by a block average
            over 3 points.

        Returns
        -------
        sems: np.ndarray
            The standardised standard error of the mean of the gradients, in kcal mol^-1 ns^(1/2).
        """
        # Check options are valid
        if origin not in ["inter", "intra"]:
            raise ValueError("origin must be either 'inter' or 'intra'")

        if origin == "inter":
            sems = self.sems_inter
        elif origin == "intra":
            sems = self.sems_intra

        # Standardise the SEMs according to the total simulation time
        sems *= _np.sqrt(self.sampling_times) # type: ignore

        if not smoothen:
            return sems # type: ignore

        # Smoothen the standard error of the mean by a block average over 3 points
        smoothened_sems = []
        max_ind = len(sems) - 1 # type: ignore
        for i, sem in enumerate(sems): # type: ignore
            # Calculate the block average for each point
            if i == 0:
                sem_smooth = (sem + self.sems_overall[i+1]) /2
            elif i == max_ind:
                sem_smooth = (sem + self.sems_overall[i-1]) /2
            else:
                sem_smooth = (sem + self.sems_overall[i+1] + self.sems_overall[i-1]) / 3 
            smoothened_sems.append(sem_smooth)
            
        smoothened_sems = _np.array(smoothened_sems)
        self._smoothened_sems = smoothened_sems
        return smoothened_sems

    def get_integrated_error(self,
                             er_type: str = "sem",
                             origin: str = "inter",
                             smoothen: bool = True)-> _np.ndarray:
        """
        Calculate the integrated standard error of the mean or root variance of the gradients
        as a function of lambda, using the trapezoidal rule.

        Parameters
        ----------
        er_type: str, optional, default="sem"
            Whether to integrate the standard error of the mean ("sem") or root 
            variance of the gradients ("root_var").
        origin: str, optional, default="inter"
            The origin of the SEM to integrate - this is ignore if er_type == "root_var".
            Can be either 'inter' or 'intra' for inter-run and intra-run SEMs respectively.
        smoothen: bool, optional, default=True
            Whether to use the smoothened SEMs or not. If False, the raw SEMs
            are used. If er_type == "root_var", this option is ignored.

        Returns
        -------
        integrated_errors: np.ndarray
            The integrated SEMs as a function of lambda, in kcal mol^-1 ns^(1/2).
        """
        # Check options are valid
        if er_type not in ["sem", "root_var"]:
            raise ValueError("er_type must be either 'sem' or 'root_var'")
        if origin not in ["inter", "intra"]:
            raise ValueError("origin must be either 'inter' or 'intra'")

        integrated_errors = []
        x_vals = self.lam_vals
        # Note that the trapezoidal rule results in some smoothing between neighbours
        # even without smoothening
        if er_type == "sem":
            y_vals = self.get_sems(origin=origin, smoothen=smoothen)
        elif er_type == "root_var":
            y_vals = _np.sqrt(self.vars_intra)
        n_vals = len(x_vals)

        for i in range(n_vals):
            # No need to worry about indexing off the end of the array with numpy
            # Note that _np.trapz(y_vals[:1], x_vals[:1]) gives 0, as required
            integrated_errors.append(_np.trapz(y_vals[:i+1], x_vals[:i+1])) #type: ignore
        
        integrated_errors = _np.array(integrated_errors)
        self._integrated_sems = integrated_errors
        return integrated_errors
    
    def calculate_optimal_lam_vals(self, 
                                   er_type: str = "sem",
                                   delta_er: _Optional[float] = None, 
                                   n_lam_vals: _Optional[int] = None,
                                   sem_origin: str = "inter",
                                   smoothen_sems: bool = True)-> _np.ndarray:
        """
        Calculate the optimal lambda values for a given number of lambda values
        to sample, using the integrated standard error of the mean of the gradients
        or root variance as a function of lambda, using the trapezoidal rule.

        Parameters
        ----------
        er_type: str, optional, default="sem"
            Whether to integrate the standard error of the mean ("sem") or root
            variance of the gradients ("root_var").
        delta_er : float, optional
            If er_type == "root_var", the desired integrated root variance of the gradients
            between each lambda value, in kcal mol^(-1). If er_type == "sem", the
            desired integrated standard error of the mean of the gradients between each lambda
            value, in kcal mol^(-1) ns^(1/2). If not provided, the number of lambda
            windows must be provided with n_lam_vals.    
        n_lam_vals : int, optional
            The number of lambda values to sample. If not provided, delta_er must be provided.
        sem_origin: str, optional, default="inter"
            The origin of the SEM to integrate. Can be either 'inter' or 'intra'
            for inter-run and intra-run SEMs respectively. If er_type == "root_var",
            this is ignored.
        smoothen_sems: bool, optional, default=True
            Whether to use the smoothened SEMs or not. If False, the raw SEMs
            are used. If True, the SEMs are smoothened by a block average over
            3 points. If er_type == "root_var", this is ignored.

        Returns
        -------
        optimal_lam_vals : np.ndarray
            The optimal lambda values to sample.
        """
        if delta_er is None and n_lam_vals is None:
            raise ValueError("Either delta_er or n_lam_vals must be provided.")
        elif delta_er is not None and n_lam_vals is not None:
            raise ValueError("Only one of delta_er or n_lam_vals can be provided.")

        # Calculate the integrated standard error of the mean of the gradients
        # as a function of lambda, using the trapezoidal rule.
        integrated_errors = self.get_integrated_error(er_type=er_type, 
                                                      origin=sem_origin,
                                                      smoothen=smoothen_sems)
        
        total_error = integrated_errors[-1]

        # If the number of lambda values is not provided, calculate it from the
        # desired integrated standard error of the mean between lam vals
        if n_lam_vals is None:
            n_lam_vals = int(total_error / delta_er) + 1

        # Convert the number of lambda values to an array of SEM values
        requested_sem_vals = _np.linspace(0, total_error, n_lam_vals)

        # For each desired SEM value, map it to a lambda value
        optimal_lam_vals = []
        for requested_sem in requested_sem_vals:
            optimal_lam_val = _np.interp(requested_sem, integrated_errors, self.lam_vals)
            optimal_lam_val = _np.round(optimal_lam_val, 3)
            optimal_lam_vals.append(optimal_lam_val)

        optimal_lam_vals = _np.array(optimal_lam_vals)
        self._optimal_lam_vals = optimal_lam_vals
        return optimal_lam_vals

    def get_predicted_overlap_mat(self, temperature: float = 298) -> _np.ndarray:
        """
        Calculate the predicted overlap matrix for the lambda windows
        based on the intra-run variances alone. The relationship is
        var_ij = beta^-2 

        Parameters
        ----------
        temperature: float, optional, default=298
            The temperature in Kelvin.

        Returns
        -------
        predicted_overlap_mat: np.ndarray
            The predicted overlap matrix for the lambda windows.
        """
        # Constants and empty matrix
        beta = (4.184 * 1000) / (_R * temperature)  # in kcal mol^-1
        predicted_overlap_mat = _np.zeros((self.n_lam, self.n_lam))

        # Start with upper triangle
        for base_index in range(self.n_lam):
            unnormalised_overlap = 1
            for i in range(self.n_lam - base_index):
                if i != 0:
                    delta_lam = self.lam_vals[base_index + i] - self.lam_vals[base_index + i - 1]
                    av_var = (self.vars_intra[base_index + i] + self.vars_intra[base_index + i - 1]) / 2
                    unnormalised_overlap /= beta * delta_lam * _np.sqrt(av_var)
                predicted_overlap_mat[base_index, base_index + i] = unnormalised_overlap

        # Copy the upper triangle to get the lower triangle, making sure not to duplicate the diagonal
        predicted_overlap_mat += predicted_overlap_mat.T - _np.diag(_np.diag(predicted_overlap_mat))
    
        # Normalise by row
        for i in range(self.n_lam):
            predicted_overlap_mat[i, :] /= predicted_overlap_mat[i, :].sum()

        return predicted_overlap_mat
