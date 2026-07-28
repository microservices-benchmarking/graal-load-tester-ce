from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import asyncio
import argparse
import pickle
import pathlib
import sys

from gos_harness import gos_logging as logging


class StoredProcedure(ABC):
    """StoredProcedure represents anything that can be stored as a pickle, loaded and run on demand"""
    def __init__(self) -> None:
        pass

    @abstractmethod
    def load(self) -> None:
        """Loads the object. This method is called after the object is loaded from the pickle."""
        pass

    @abstractmethod
    async def run(self) -> Any:
        """On demand, the object is run and its results are pickled as an output.

        The result is stored in the file with an extension ".out.pickle"
        """
        pass


def main(argv: list[str] = sys.argv) -> None:
    parser = argparse.ArgumentParser(description='Remote Procedure Call for the Graal Load Tester')
    parser.add_argument("procedure", help="path to the pickled state", type=pathlib.Path)
    parser.add_argument("--log-to", type=str, default="stdout", help='Logs the output to different streams. Each stream is delimited by comma, supported logs are: stdout, file:<PATH>')
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="INFO", help='Sets the log level')
    args = parser.parse_args(argv[1:])
    logging.setup_logger(args.log_to, args.log_level)

    logging.log(f"Loading remote procedure from {args.procedure}")
    with open(args.procedure, 'rb') as procedure_file:
        stored_procedure = pickle.load(procedure_file)

    if not isinstance(stored_procedure, StoredProcedure):
        logging.die("wrong pickle file: does not contain StoredProcedure")

    # Object in the pickled is loaded and then immediately run.
    stored_procedure.load()
    result = asyncio.run(stored_procedure.run())

    # The results are stored in the file with .out.pickle suffix
    target_file = args.procedure.with_suffix(".out.pickle")
    with open(target_file, 'wb') as output_pickle:
        pickle.dump(result, output_pickle)

    logging.log(f"Output saved to '{target_file}'")
