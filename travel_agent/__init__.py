from importlib import import_module


def __getattr__(name):
    if name == "agent":
        return import_module(".agent", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
