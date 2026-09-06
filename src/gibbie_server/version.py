from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("sdl2-gibbie-server")
except PackageNotFoundError:  # source tree without an installed distribution
    __version__ = "0.0.0+unknown"
