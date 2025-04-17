"""
Module initialization for the promptModel package.
This module sets up the public interface by importing and exposing the
MultiModalPrompting class, which provides functionality for handling
multi-modal prompting in applications. It is designed to help integrate
different data modalities seamlessly into the prompting workflow.
"""

from .fastmcp_wrapper import FastMCPWrapper

__all__=[
    "FastMCPWrapper"
]