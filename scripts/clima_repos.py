"""CliMA-org Julia repos the fetch stages track.

Names are without the ``.jl`` suffix. Every fetch stage uses this one list,
so they all cover the same repos. Repos under spin-off orgs, e.g.,
NumericalEarth, aren't CliMA's and aren't tracked.
"""

CLIMA_REPOS = [
    "ClimaAtmos",
    "ClimaCore",
    "ClimaParams",
    "Thermodynamics",
    "CloudMicrophysics",
    "ClimaCoupler",
    "ClimaLand",
    "ClimaOcean",
    "SurfaceFluxes",
    "Insolation",
    "RRTMGP",
    "ClimaUtilities",
    "ClimaDiagnostics",
    "ClimaAnalysis",
    "Oceananigans",
    "ClimaSeaIce",
    "SeawaterPolynomials",
    "CubedSphere",
    "ClimaTimeSteppers",
    "ClimaComms",
    "UnrolledUtilities",
    "MultiBroadcastFusion",
    "LazyBroadcast",
    "NullBroadcasts",
    "StructuredPrinting",
    "OperatorFlux",
    "ClimaInterpolations",
    "RootSolvers",
    "ArtifactWrappers",
    "ClimaEarth",
    "ClimaViz",
    "RandomFeatures",
    "EnsembleKalmanProcesses",
    "CalibrateEmulateSample",
    "ClimaCalibrate",
    "GriddingMachine",
    "Cloudy",
    "KinematicDriver",
    "AtmosphericProfilesLibrary",
    "GilbertCurves",
    "OrthogonalSphericalShellGrids",
    "Land",
]

# Repos whose GitHub name isn't ``<name>.jl``. These aren't registered Julia
# packages under that name, so the registry, download, and code-search
# stages skip them.
REPO_NAMES = {"Land": "Land"}


# Spin-offs run by CliMA developers. Their repos aren't CliMA products and
# aren't tracked, and their use of CliMA packages isn't outside adoption.
AFFILIATED_ORGS = {"NumericalEarth"}


def repo_slug(name: str) -> str:
    """The repo's ``owner/name`` on GitHub, e.g., ``CliMA/ClimaCore.jl``."""
    return "CliMA/" + REPO_NAMES.get(name, f"{name}.jl")


def is_package(name: str) -> bool:
    """Whether ``name`` is a Julia package named after its repo."""
    return name not in REPO_NAMES
