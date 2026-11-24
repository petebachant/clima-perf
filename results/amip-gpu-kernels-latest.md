# Most expensive GPU kernels in the nightly AMIP

From the 2026-11-24 nightly, [build 1649](https://buildkite.com/clima/climacoupler-coarse-nightly-amip/builds/1649) of `climacoupler-coarse-nightly-amip` at ClimaCoupler.jl `a678b471`: the Nsight Systems kernel summary of the profiled steps of the `amip_progedmf_1m_land_he16` configuration, 4,612 ms of GPU kernel time in all. The top 25 of 301 kernels are shown; `results/amip-gpu-kernels-latest.csv` has them all.

| # | Kernel | Source | GPU time (%) | Total (ms) | Calls | Avg (µs) |
|--:|---|---|--:|--:|--:|--:|
| 1 | `rte_lw_2stream_solve_both_skies_CUDA` |  | 14.3 | 659.0 | 1 | 658,988.5 |
| 2 | `rte_sw_2stream_solve_both_skies_CUDA` |  | 13.2 | 606.8 | 1 | 606,810.0 |
| 3 | `set_environment_1m_tendency` | `ClimaAtmos src_cache_microphysics_cache.jl:1116` | 10.8 | 497.4 | 80 | 6,217.5 |
| 4 | `update_updraft_microphysics_tendency_cache` |  | 5.5 | 255.8 | 160 | 1,598.7 |
| 5 | `set_precipitation_velocities` | `ClimaAtmos src_cache_microphysics_cache.jl:242` | 4.1 | 188.2 | 200 | 941.1 |
| 6 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:790` | 4.0 | 184.0 | 480 | 383.4 |
| 7 | `copyto_by_slices` | `ClimaCore src_Operators_broadcast.jl:293` | 3.8 | 174.0 | 3,840 | 45.3 |
| 8 | `field_matrix_solve` |  | 3.5 | 159.4 | 100 | 1,593.7 |
| 9 | `set_covariance_cache_loop` | `ClimaAtmos src_parameterized_tendencies_microphysics_cloud_fraction.jl:793` | 2.2 | 103.1 | 120 | 859.2 |
| 10 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:854` | 1.9 | 89.4 | 480 | 186.3 |
| 11 | `fill_column_max` | `ClimaAtmos src_cache_microphysics_cache.jl:93` | 1.9 | 87.0 | 1,080 | 80.6 |
| 12 | `solve_sgs__a_implicit_stage_analytic` | `ClimaAtmos src_prognostic_equations_implicit_initialize_implicit_problem.jl:798` | 1.5 | 71.2 | 240 | 296.7 |
| 13 | `horizontal_advection_tendency` |  | 1.4 | 65.2 | 40 | 1,630.0 |
| 14 | `edmfx_sgs_diffusive_flux_tendency` | `ClimaAtmos src_prognostic_equations_edmfx_sgs_flux.jl:402` | 1.4 | 64.5 | 40 | 1,611.5 |
| 15 | `set_implicit_precomputed_quantities_core` |  | 1.3 | 57.9 | 320 | 180.9 |
| 16 | `set_prognostic_edmf_precomputed_quantities_environment` |  | 1.1 | 49.1 | 160 | 306.6 |
| 17 | `update_sgs_massflux_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:2625` | 1.0 | 44.1 | 40 | 1,102.2 |
| 18 | `update_sgs_advection_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:1782` | 0.9 | 42.8 | 40 | 1,069.3 |
| 19 | `edmfx_sgs_vertical_advection_tendency` | `ClimaAtmos src_prognostic_equations_advection.jl:427` | 0.8 | 38.2 | 40 | 954.9 |
| 20 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:155` | 0.8 | 37.9 | 840 | 45.1 |
| 21 | `(anonymous)` | `ClimaAtmos src_prognostic_equations_implicit_initialize_implicit_problem.jl:1281` | 0.8 | 37.0 | 480 | 77.0 |
| 22 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:140` | 0.8 | 36.9 | 840 | 43.9 |
| 23 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:149` | 0.8 | 36.5 | 760 | 48.0 |
| 24 | `sweep_sgs_updraft_tracers` |  | 0.8 | 35.7 | 80 | 445.9 |
| 25 | `initialize_implicit_stage_problem` |  | 0.8 | 35.7 | 440 | 81.1 |
