# We use the @tool decorator directly from langchain, 'as is'.
# The plugin system imports it from here (cat.decorators module), as it will be possible to extend it later on
# from langchain.agents import tool


def prompt(function):
    def wrapper():
        # print("DECORATING", function.__name__)
        return function()

    return wrapper


def hook(*args, **kwargs):
    def wrapper(func, priority=1):
        # print("DECORATING", function.__name__)
        return func()

    return wrapper


def tool(s):
    pass