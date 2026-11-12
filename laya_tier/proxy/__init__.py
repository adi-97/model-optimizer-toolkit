from .config import Budget, Config, Route, load_config

__all__ = ["Budget", "Config", "Route", "load_config", "create_app"]


def create_app(*args, **kwargs):
    from .server import create_app as _create_app

    return _create_app(*args, **kwargs)
