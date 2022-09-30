#!/bin/sh
# l5 dimensions [126,  88,   6]

max=62
for idx in `seq 0 $max`
do
    i=`expr $idx \* 2`
    j=`expr $j + 2`
    # echo $i
    # echo $j
    python meshing_batch.py --layer 5 --chunk_start $i 0 0 --chunk_end $j 88 6 --mip 2 --cg_name h01_full0_v2 --queue_name https://sqs.us-east-2.amazonaws.com/622009480892/cave-meshing-sqs
done