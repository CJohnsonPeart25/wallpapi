"""The taste engine: code only, data in and data out.

It opens no file or connection, starts no thread and makes no network call, and imports nothing from the rest
of wallpapi, which hosts it, conforms to its data shapes and owns all storage, threads and network.
`tests/test_taste_boundary.py` holds both rules.
"""
