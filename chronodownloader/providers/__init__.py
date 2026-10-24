"""Library adapters behind one typed protocol.

:mod:`.base` defines the :class:`~.base.Provider` protocol, its records and
results; :mod:`.registry` builds the adapters of a run lazily from settings;
:mod:`.iiif_download` and :mod:`.staging` are the shared download helpers.
Adapters never import the output or matching layers.
"""
