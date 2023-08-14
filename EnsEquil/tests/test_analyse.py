"""Testing the analysis module."""

import os
from tempfile import TemporaryDirectory

import numpy as np
import pytest

import EnsEquil as ee

from ..analyse.detect_equil import (
    check_equil_multiwindow_gelman_rubin,
    check_equil_multiwindow_modified_geweke,
    check_equil_multiwindow_paired_t,
)
from ..analyse.process_grads import get_time_series_multiwindow
from .fixtures import restrain_stage


def test_analysis_all_runs(restrain_stage):
    """Check that the analysis works on all runs."""
    res, err = restrain_stage.analyse()
    assert res.mean() == pytest.approx(1.5978, abs=1e-2)
    assert err.mean() == pytest.approx(0.0254, abs=1e-3)


def test_analysis_all_runs_fraction(restrain_stage):
    """Check that the analysis works on all runs."""
    res, err = restrain_stage.analyse(fraction=0.5)
    assert res.mean() == pytest.approx(1.6252, abs=1e-2)
    assert err.mean() == pytest.approx(0.0366, abs=1e-3)


def test_get_results_df(restrain_stage):
    """Check that the results dataframe is correctly generated."""
    # Re-analyse to ensure that the order of the tests doesn't matter
    res, err = restrain_stage.analyse()
    df = restrain_stage.get_results_df()
    # Check that the csv has been output
    assert os.path.exists(os.path.join(restrain_stage.output_dir, "results.csv"))
    # Check that the results are correct
    assert df.loc["restrain_stage"]["dg / kcal mol-1"] == pytest.approx(1.6, abs=1e-1)
    assert df.loc["restrain_stage"]["dg_95_ci / kcal mol-1"] == pytest.approx(
        0.21, abs=1e-2
    )
    assert df.loc["restrain_stage"]["tot_simtime / ns"] == pytest.approx(6.0, abs=1e-1)
    assert df.loc["restrain_stage"]["tot_gpu_time / GPU hours"] == pytest.approx(
        1, abs=1e-0
    )


def test_analysis_subselection_runs(restrain_stage):
    """Check that the analysis works on a subselection of runs."""
    res, err = restrain_stage.analyse(run_nos=[1, 2, 4])
    assert res.mean() == pytest.approx(1.6154, abs=1e-2)
    assert err.mean() == pytest.approx(0.0257, abs=1e-3)


def test_convergence_analysis(restrain_stage):
    """Test the convergence analysis."""
    expected_results = np.array(
        [
            [
                1.811328,
                1.79284,
                1.686816,
                1.645066,
                1.603964,
                1.560607,
                1.560784,
                1.591202,
                1.579906,
                1.595907,
                1.590091,
                1.597802,
                1.621957,
                1.625856,
                1.62638,
                1.626285,
                1.627279,
                1.636536,
                1.631825,
                1.624827,
            ],
            [
                1.80024,
                1.720471,
                1.669383,
                1.679446,
                1.651117,
                1.667022,
                1.695923,
                1.717015,
                1.742829,
                1.745102,
                1.775038,
                1.769987,
                1.764087,
                1.776267,
                1.787469,
                1.802154,
                1.802192,
                1.805146,
                1.805154,
                1.803477,
            ],
            [
                1.465609,
                1.347802,
                1.370879,
                1.393137,
                1.361814,
                1.361366,
                1.370421,
                1.385677,
                1.391987,
                1.386363,
                1.404679,
                1.407778,
                1.416555,
                1.412339,
                1.416453,
                1.411892,
                1.4214,
                1.426488,
                1.429623,
                1.426475,
            ],
            [
                1.342867,
                1.367449,
                1.358504,
                1.407188,
                1.431588,
                1.435713,
                1.437941,
                1.428834,
                1.424351,
                1.412054,
                1.405822,
                1.403497,
                1.402927,
                1.411375,
                1.414117,
                1.40972,
                1.414707,
                1.413852,
                1.413605,
                1.41811,
            ],
            [
                1.561257,
                1.618,
                1.640034,
                1.620545,
                1.625897,
                1.608938,
                1.632432,
                1.679825,
                1.697756,
                1.714111,
                1.715355,
                1.712736,
                1.722261,
                1.715033,
                1.700635,
                1.712187,
                1.711371,
                1.713132,
                1.717778,
                1.716112,
            ],
        ]
    )
    stage = restrain_stage
    _, free_energies = stage.analyse_convergence()
    assert np.allclose(free_energies, expected_results, atol=1e-2)


def test_get_time_series_multiwindow(restrain_stage):
    """Check that the time series are correctly extracted/ combined."""
    # Check that this fails if we haven't set equil times
    overall_dgs, overall_times = get_time_series_multiwindow(
        lambda_windows=restrain_stage.lam_windows,
        equilibrated=True,
        run_nos=[1, 2],
    )

    # Check that the output has the correct shape
    assert overall_dgs.shape == (2, 100)
    assert overall_times.shape == (2, 100)

    # Check that the total time is what we expect
    tot_simtime = restrain_stage.get_tot_simtime(run_nos=[1])
    assert overall_times[0][-1] == pytest.approx(tot_simtime, abs=1e-2)

    # Check that the output values are correct
    assert overall_dgs.mean(axis=0)[-1] == pytest.approx(1.7751, abs=1e-2)
    assert overall_times.sum(axis=0)[-1] == pytest.approx(2.4, abs=1e-2)


def test_geweke(restrain_stage):
    """Test the modified Geweke equilibration analysis."""
    with TemporaryDirectory() as tmpdir:
        (
            equilibrated,
            fractional_equil_time,
        ) = check_equil_multiwindow_modified_geweke(
            lambda_windows=restrain_stage.lam_windows,
            output_dir=tmpdir,
            intervals=10,
            p_cutoff=0.4,
        )

        assert equilibrated
        assert fractional_equil_time == pytest.approx(0.0048, abs=1e-2)


def test_paired_t(restrain_stage):
    """Test the paired t-test equilibration analysis."""
    with TemporaryDirectory() as tmpdir:
        (
            equilibrated,
            fractional_equil_time,
        ) = check_equil_multiwindow_paired_t(
            lambda_windows=restrain_stage.lam_windows,
            output_dir=tmpdir,
            intervals=10,
            p_cutoff=0.05,
        )

        assert equilibrated
        assert fractional_equil_time == pytest.approx(0.0048, abs=1e-2)


def test_gelman_rubin(restrain_stage):
    """Test the Gelman-Rubin convergence analysis."""
    with TemporaryDirectory() as tmpdir:
        rhat_dict = check_equil_multiwindow_gelman_rubin(
            lambda_windows=restrain_stage.lam_windows,
            output_dir=tmpdir,
        )

        expected_rhat_dict = {
            0.0: 1.0496660104040842,
            0.125: 1.0122689789813877,
            0.25: 1.0129155249894615,
            0.375: 1.0088598498180925,
            0.5: 1.020819039702674,
            1.0: 1.0095474751197715,
        }
        assert rhat_dict == expected_rhat_dict
