#!/bin/bash

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Pulls down crash data for specified crash ids, syncs to the cloud storage
# bucket, and sends the crash ids to the queue.
#
# Usage: ./bin/process_crashes.sh
#
# You can use it with fetch_crashids. For example:
#
#     socorro-cmd fetch_crashids --num=1 | ./bin/process_crashes.sh
#
# Make sure to run the processor to do the actual processing.
#
# Note: This should be called from inside a container.

set -euo pipefail

DATADIR=./crashdata_tryit_tmp

function cleanup {
    # Cleans up files generated by the script
    rm -rf "${DATADIR}"
}

# Set up cleanup function to run on script exit
trap cleanup EXIT

if [[ $# -eq 0 ]]; then
    if [ -t 0 ]; then
        # If stdin is a terminal, then there's no input
        echo "Usage: process_crashes.sh CRASHID [CRASHID ...]"
        exit 1
    fi

    # stdin is not a terminal, so pull the args from there
    set -- ${@:-$(</dev/stdin)}
fi

mkdir "${DATADIR}" || echo "${DATADIR} already exists."

# Pull down the data for all crashes
./socorro-cmd fetch_crash_data "${DATADIR}" $@

# Make the bucket and sync contents
./socorro-cmd gcs create "${CRASHSTORAGE_GCS_BUCKET}"
./socorro-cmd gcs upload "${DATADIR}" "${CRASHSTORAGE_GCS_BUCKET}"
./socorro-cmd gcs list_objects "${CRASHSTORAGE_GCS_BUCKET}"

# Add crash ids to queue
./socorro-cmd pubsub publish "${PUBSUB_PROJECT_ID}" "${PUBSUB_STANDARD_TOPIC_NAME}" $@

# Print urls to make it easier to look at them
for crashid in "$@"
do
    echo "Check webapp: http://localhost:8000/report/index/${crashid}"
done

echo "The crash(es) has/have been queued."
echo "To process and view them, start up the processor and webapp."
