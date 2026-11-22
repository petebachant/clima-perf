# Most expensive GPU kernels in the nightly AMIP

From the 2026-11-19 nightly, [build 1637](https://buildkite.com/clima/climacoupler-coarse-nightly-amip/builds/1637) of `climacoupler-coarse-nightly-amip` at ClimaCoupler.jl `be5e1fab`: the Nsight Systems kernel summary of the profiled steps of the `amip_progedmf_1m_land_he16` configuration, 6,181 ms of GPU kernel time in all. The top 25 of 309 kernels are shown; `results/amip-gpu-kernels-latest.csv` has them all.

| # | Kernel | Source | GPU time (%) | Total (ms) | Calls | Avg (µs) |
|--:|---|---|--:|--:|--:|--:|
| 1 | `set_environment_1m_tendency` | `ClimaAtmos src_cache_microphysics_cache.jl:1116` | 19.9 | 1,227.1 | 160 | 7,669.2 |
| 2 | `rte_lw_2stream_solve_both_skies_CUDA` |  | 10.7 | 661.7 | 1 | 661,659.7 |
| 3 | `rte_sw_2stream_solve_both_skies_CUDA` |  | 9.8 | 605.0 | 1 | 605,035.0 |
| 4 | `update_updraft_microphysics_tendency_cache` |  | 7.6 | 469.8 | 160 | 2,936.0 |
| 5 | `copyto_by_slices` | `ClimaCore src_Operators_broadcast.jl:293` | 5.9 | 367.5 | 6,100 | 60.2 |
| 6 | `set_precipitation_velocities` | `ClimaAtmos src_cache_microphysics_cache.jl:242` | 3.9 | 242.1 | 200 | 1,210.4 |
| 7 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:780` | 3.1 | 191.6 | 480 | 399.1 |
| 8 | `run_field_matrix_solver` | `ClimaCore src_MatrixFields_field_matrix_solver.jl:377` | 2.5 | 154.7 | 1,920 | 80.6 |
| 9 | `set_covariance_cache_loop` | `ClimaAtmos src_parameterized_tendencies_microphysics_cloud_fraction.jl:793` | 2.0 | 121.4 | 120 | 1,011.8 |
| 10 | `set_microphysics_tendency_cache` | `ClimaAtmos src_cache_microphysics_cache.jl:1590` | 1.9 | 120.0 | 40 | 2,999.9 |
| 11 | `solve_sgs__a_implicit_stage_analytic` | `ClimaAtmos src_prognostic_equations_implicit_initialize_implicit_problem.jl:787` | 1.4 | 85.3 | 240 | 355.6 |
| 12 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:844` | 1.3 | 82.1 | 480 | 171.1 |
| 13 | `sweep_sgs_updraft_tracers` |  | 1.2 | 73.9 | 560 | 131.9 |
| 14 | `updraft_sedimentation_rate` | `ClimaAtmos src_prognostic_equations_advection.jl:754` | 1.2 | 73.0 | 840 | 86.9 |
| 15 | `edmfx_sgs_diffusive_flux_tendency` | `ClimaAtmos src_prognostic_equations_edmfx_sgs_flux.jl:402` | 1.0 | 64.6 | 40 | 1,614.9 |
| 16 | `horizontal_advection_tendency` |  | 1.0 | 59.5 | 40 | 1,487.6 |
| 17 | `set_implicit_precomputed_quantities_core` |  | 0.9 | 57.3 | 320 | 179.0 |
| 18 | `update_sgs_advection_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:1782` | 0.8 | 50.9 | 40 | 1,273.6 |
| 19 | `set_prognostic_edmf_precomputed_quantities_environment` |  | 0.8 | 50.5 | 160 | 315.5 |
| 20 | `update_sgs_massflux_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:2625` | 0.7 | 45.3 | 40 | 1,131.5 |
| 21 | `(anonymous)` | `ClimaCore src_MatrixFields_field_name_dict.jl:1071` | 0.6 | 40.1 | 1,360 | 29.5 |
| 22 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:155` | 0.6 | 38.1 | 840 | 45.3 |
| 23 | `edmfx_sgs_vertical_advection_tendency` | `ClimaAtmos src_prognostic_equations_advection.jl:425` | 0.6 | 37.6 | 40 | 939.7 |
| 24 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:140` | 0.6 | 37.1 | 840 | 44.1 |
| 25 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:149` | 0.6 | 36.6 | 760 | 48.2 |
