# Most expensive GPU kernels in the nightly AMIP

From the 2026-11-26 nightly, [build 1659](https://buildkite.com/clima/climacoupler-coarse-nightly-amip/builds/1659) of `climacoupler-coarse-nightly-amip` at ClimaCoupler.jl `3d0efe91`: the Nsight Systems kernel summary of the profiled steps of the `amip_progedmf_1m_land_he16` configuration, 4,105 ms of GPU kernel time in all. The top 25 of 307 kernels are shown; `results/amip-gpu-kernels-latest.csv` has them all.

| # | Kernel | Source | GPU time (%) | Total (ms) | Calls | Avg (µs) |
|--:|---|---|--:|--:|--:|--:|
| 1 | `set_environment_1m_tendency` | `ClimaAtmos src_cache_microphysics_cache.jl:1227` | 12.1 | 497.9 | 80 | 6,223.9 |
| 2 | `set_precipitation_velocities` | `ClimaAtmos src_cache_microphysics_cache.jl:336` | 10.8 | 445.1 | 240 | 1,854.5 |
| 3 | `update_updraft_microphysics_tendency_cache` |  | 6.2 | 255.8 | 160 | 1,598.6 |
| 4 | `lw_group_optics_CUDA` |  | 5.8 | 239.1 | 16 | 14,943.9 |
| 5 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:790` | 4.4 | 181.0 | 480 | 377.0 |
| 6 | `copyto_by_slices` | `ClimaCore src_Operators_broadcast.jl:310` | 4.2 | 174.1 | 3,840 | 45.3 |
| 7 | `field_matrix_solve` |  | 3.9 | 159.7 | 100 | 1,597.0 |
| 8 | `sw_group_optics_CUDA` |  | 2.7 | 109.9 | 14 | 7,851.4 |
| 9 | `set_covariance_cache_loop` | `ClimaAtmos src_parameterized_tendencies_microphysics_cloud_fraction.jl:793` | 2.5 | 103.3 | 120 | 861.1 |
| 10 | `sedimentation_rate_pair` | `ClimaAtmos src_prognostic_equations_advection.jl:854` | 2.2 | 88.7 | 480 | 184.8 |
| 11 | `fill_column_max` | `ClimaAtmos src_cache_microphysics_cache.jl:93` | 2.1 | 87.1 | 1,080 | 80.7 |
| 12 | `lw_group_sweeps_CUDA` |  | 2.1 | 85.1 | 16 | 5,320.8 |
| 13 | `solve_sgs__a_implicit_stage_analytic` | `ClimaAtmos src_prognostic_equations_implicit_initialize_implicit_problem.jl:804` | 1.7 | 71.3 | 240 | 297.0 |
| 14 | `horizontal_advection_tendency` |  | 1.6 | 66.1 | 40 | 1,653.2 |
| 15 | `edmfx_sgs_diffusive_flux_tendency` | `ClimaAtmos src_prognostic_equations_edmfx_sgs_flux.jl:402` | 1.6 | 64.6 | 40 | 1,614.4 |
| 16 | `sw_group_sweeps_CUDA` |  | 1.5 | 60.5 | 14 | 4,322.1 |
| 17 | `set_implicit_precomputed_quantities_core` |  | 1.4 | 58.0 | 320 | 181.2 |
| 18 | `set_prognostic_edmf_precomputed_quantities_environment` |  | 1.2 | 49.1 | 160 | 306.6 |
| 19 | `update_sgs_massflux_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:2622` | 1.1 | 44.0 | 40 | 1,100.7 |
| 20 | `update_sgs_advection_jacobian` | `ClimaAtmos src_prognostic_equations_implicit_manual_sparse_jacobian.jl:1779` | 1.1 | 43.1 | 40 | 1,077.8 |
| 21 | `edmfx_sgs_vertical_advection_tendency` | `ClimaAtmos src_prognostic_equations_advection.jl:427` | 0.9 | 38.0 | 40 | 950.2 |
| 22 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:155` | 0.9 | 37.8 | 840 | 45.0 |
| 23 | `(anonymous)` | `ClimaAtmos src_prognostic_equations_implicit_initialize_implicit_problem.jl:1287` | 0.9 | 37.3 | 480 | 77.8 |
| 24 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:140` | 0.9 | 36.9 | 840 | 43.9 |
| 25 | `weighted_dss_internal` | `ClimaCore src_Spaces_dss.jl:149` | 0.9 | 36.5 | 760 | 48.1 |
