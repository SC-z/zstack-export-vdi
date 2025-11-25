#!/bin/bash

for i in {0..77}; do
    missing=$(LD_LIBRARY_PATH=../lib:$LD_LIBRARY_PATH ldd ../bin/qemu-img | grep not | awk '{print $1}')

    if [ -n "$missing" ]; then
        for n in $missing; do
            cp ../lib/sc/"$n" ../lib/
            echo "cp ../lib/sc/$n ../lib/"
        done
    else
        echo "copy lib done."
    fi
done