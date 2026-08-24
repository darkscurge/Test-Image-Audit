#!/bin/sh
set -e
docker build -t product-image-audit .
docker run --rm -p 8000:8000 product-image-audit
