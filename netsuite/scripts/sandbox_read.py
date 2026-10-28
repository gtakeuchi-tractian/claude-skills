#!/usr/bin/env python3
"""Isolated sandbox reads using the shared read-only transport and signer."""

import sys

import prod_read


def main(argv=None):
    return prod_read.main(argv, environment="sandbox")


if __name__ == "__main__":
    sys.exit(main())
