#!/bin/bash
# 1. The current BIT repo will be cloned into a temporary directory.
# 2. The debian folder and helper scripts are copied into that folder.
# In the end "makedeb.sh" can be executed to build the deb file.
# See Issue 1548 in "bit-team/backintime" repo for more details.
BIT_GIT_URL='https://github.com/bit-team/backintime.git'

# Create temp folder
TEMP_DIR=$(mktemp --directory -t backintime-XXXXX)

# # Remove temp folder on script exit
# trap 'rm --force --recursive --verbose "$TEMP_DIR"' EXIT

echo "Temporary directory $TEMP_DIR created"

git clone $BIT_GIT_URL $TEMP_DIR
cp --verbose --recursive * $TEMP_DIR

ls -l $TEMP_DIR

echo "Please navigate into $TEMP_DIR and run makedeb.sh"

