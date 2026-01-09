from setuptools import setup

setup(
    name="llm-sanitizer",
    version="0.1.0",
    description="A FUSE filesystem to obfuscate sensitive data for LLM agents",
    author="Evgeny Balyakin",
    py_modules=["obfuscate_runner"],
    install_requires=[
        "fusepy",
    ],
    entry_points={
        "console_scripts": [
            "llm-sanitizer=obfuscate_runner:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: MacOS",
    ],
)
