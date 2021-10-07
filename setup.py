from setuptools import find_packages, setup

with open("README.md") as fh:
    long_description = fh.read()
with open("semver.txt") as fh:
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
    packages=find_packages(),
    include_package_data=True,
    install_requires=[],
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.7",
        "Topic :: Software Development :: Documentation",
    ],
    entry_points={"console_scripts": ["kdlreformat = kdl:cli"]},
)
