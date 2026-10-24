"""The output root: its store and the names of its folders and files.

:mod:`.store` holds ``chronodownloader.sqlite``, the only status record of an
output root (works, candidates, download attempts, files), with per-work
leases and the lease-checked commit of staged downloads; :mod:`.paths` names
the work folders and the final files inside them.
"""
