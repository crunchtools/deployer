"""Deterministic deploy robot for the crunchtools fleet.

Drains the RT ``Deploy`` queue, deploys by commit, verifies, and rolls back
once if the result does not hold. No model runs anywhere in this process: it is
started by a unit, and everything it is willing to do is fixed by reviewed
config.

The capability boundary is the point of the design, so it is stated here as
well as enforced in :mod:`deployer.podman`: this package can pull, inspect,
tag, untag, remove and list images, and it can restart or start a unit named in
the registry. It cannot create, start, run or exec a *container*. A process
that cannot start a container cannot mount a live volume, and therefore cannot
relabel one -- which is the class of mistake a deploy robot must not be able to
make.
"""

__all__ = ["__version__"]

__version__ = "0.4.0"
