# Most expensive GPU kernels in the nightly AMIP

From the 2026-11-27 nightly, [build 1661](https://buildkite.com/clima/climacoupler-coarse-nightly-amip/builds/1661) of `climacoupler-coarse-nightly-amip` at ClimaCoupler.jl `83e36da9`: the Nsight Systems kernel summary of the profiled steps of the `amip_progedmf_1m_land_he16` configuration, 4,119 ms of GPU kernel time in all. The top 25 of 305 kernels are shown; `results/amip-gpu-kernels-latest.csv` has them all.

| # | Kernel | Source | GPU time (%) | Total (ms) | Calls | Avg (µs) |
|--:|---|---|--:|--:|--:|--:|
| 1 | `set_environment_1m_tendency` | `ClimaAtmos src_cache_microphysics_cache.jl:1375` | 12.1 | 500.4 | 80 | 6,254.7 |
| 2 | `set_precipitation_velocities` | `ClimaAtmos src_cache_microphysics_cache.jl:336` | 10.9 | 447.4 | 240 | 1,864.1 |
| 3 | `update_updraft_microphysics_tendency_cache` |  | 6.2 | 257.1 | 160 | 1,607.2 |
| 4 | `lw_group_optics_CUDA` |  | 5.8 | 239.1 | 16 | 14,944.6 |
| 5 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:790` | 4.4 | 180.9 | 480 | 376.9 |
| 6 | `copyto_by_slices` | `ClimaCore src_Operators_broadcast.jl:310` | 4.2 | 174.7 | 3,840 | 45.5 |
| 7 | `field_matrix_solve` |  | 3.9 | 160.2 | 100 | 1,601.5 |
| 8 | `sw_group_optics_CUDA` |  | 2.7 | 110.7 | 14 | 7,903.7 |
| 9 | `set_covariance_cache_loop` | `ClimaAtmos src_parameterized_tendencies_microphysics_cloud_fraction.jl:793` | 2.5 | 103.6 | 120 | 863.5 |
| 10 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:854` | 2.2 | 89.1 | 480 | 185.7 |
| 11 | `fill_column_max` | `ClimaAtmos src_cache_microphysics_cache.jl:93` | 2.1 | 87.5 | 1,080 | 81.0 |
| 12 | `lw_group_sweeps_CUDA` |  | 2.1 | 85.0 | 16 | 5,311.4 |
| 13 | `solve_sgs__a_implicit_stage_analytic` | `ClimaAtmos src_prognostic_equations_implicit_initialize_implicit_problem.jl:804` | 1.7 | 71.7 | 240 | 298.8 |
| 14 | `horizontal_advection_tendency` |  | 1.6 | 66.2 | 40 | 1,655.0 |
| 15 | `edmfx_sgs_diffusive_flux_tendency` | `ClimaAtmos src_prognostic_equations_edmfx_sgs_flux.jl:402` | 1.6 | 64.7 | 40 | 1,617.6 |
| 16 | `sw_group_sweeps_CUDA` |  | 1.5 | 60.5 | 14 | 4,318.9 |
| 17 | `set_implicit_precomputed_quantities_core` |  | 1.4 | 58.0 | 320 | 181.3 |
| 18 | `set_prognostic_edmf_precomputed_quantities_environment` |  | 1.2 | 49.3 | 160 | 308.0 |
| 19 | `update_sgs_massflux_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:2622` | 1.1 | 44.0 | 40 | 1,101.1 |
| 20 | `update_sgs_advection_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:1779` | 1.0 | 43.2 | 40 | 1,079.9 |
| 21 | `edmfx_sgs_vertical_advection_tendency` | `ClimaAtmos src_prognostic_equations_advection.jl:427` | 0.9 | 38.1 | 40 | 953.6 |
| 22 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:155` | 0.9 | 38.0 | 840 | 45.2 |
| 23 | `(anonymous)` | `ClimaAtmos src_prognostic_equations_implicit_initialize_implicit_problem.jl:1287` | 0.9 | 37.8 | 480 | 78.7 |
| 24 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:140` | 0.9 | 37.1 | 840 | 44.1 |
| 25 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:149` | 0.9 | 36.7 | 760 | 48.3 |
