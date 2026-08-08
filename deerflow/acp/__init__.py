"""Local ACP v1 adapter for the embedded DeerFlow runtime.

The package initializer intentionally performs no runtime imports.  ACP
clients commonly launch agents with the client's workspace as the process
cwd; keeping this module inert lets ``deerflow.acp.__main__`` switch to the
DeerFlow project root before configuration or ``.env`` discovery can occur.
"""
