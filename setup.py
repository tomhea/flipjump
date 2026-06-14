"""
the setuptools build entry: declares the optional native interpreter engine (_fjcore).

the extension is best-effort - if no C compiler is available (a plain sdist install on an
exotic platform), the build falls back to a pure-python package and the interpreter uses
the pure-python fast loop. the prebuilt wheels (built in CI with cibuildwheel) ship the
compiled engine, tagged abi3 so one wheel per platform covers every CPython >= 3.10.
"""

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

SKIP_WARNING = (
    'WARNING: building the optional native interpreter engine (_fjcore) failed: {error!r}. '
    'flipjump will run with the pure-python interpreter (the native engine is ~100x faster - '
    'install a C compiler and reinstall to enable it).'
)


class OptionalBuildExt(build_ext):  # type: ignore[misc]
    """build the native engine if possible; never fail the install over it."""

    def run(self) -> None:
        try:
            super().run()
        except Exception as build_error:
            print(SKIP_WARNING.format(error=build_error))

    def build_extension(self, ext: Extension) -> None:
        try:
            super().build_extension(ext)
        except Exception as build_error:
            print(SKIP_WARNING.format(error=build_error))


setup(
    ext_modules=[
        Extension(
            'flipjump.interpreter._fjcore',
            sources=['flipjump/interpreter/_fjcore.c'],
            py_limited_api=True,  # abi3: one binary covers every CPython >= 3.10
        )
    ],
    cmdclass={'build_ext': OptionalBuildExt},
    options={'bdist_wheel': {'py_limited_api': 'cp310'}},
)
