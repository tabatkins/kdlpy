from setuptools import find_namespace_packages, setup

with open("README.md", encoding="utf-8") as fh:
    long_description = fh.read()
with open("semver.txt", encoding="utf-8") as fh:
    semver = fh.read().strip()
"""
with open("requirements.txt") as fh:
    install_requires = [x.strip() for x in fh.read().strip().split("\n")]
"""

setup(
    name="kdl-py",
    version=semver,
    author="Tab Atkins-Bittner",
    description="A parser for the KDL language.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/tabatkins/kdlpy/",
    license_expression="MIT",
    license_files=["LICENSE"],
    packages=find_namespace_packages(include=["kdl*"]),
    package_data={"kdl": ["py.typed"]},
    include_package_data=True,
    install_requires=[],
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.7",
        "Topic :: Software Development :: Documentation",
    ],
    entry_points={"console_scripts": ["kdlreformat = kdl:cli"]},
)
