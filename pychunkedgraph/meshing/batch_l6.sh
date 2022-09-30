#!/bin/sh
# [63, 44,  3])

max=20
for idx in `seq 0 $max`
do
    i=`expr $idx \* 3`
    j=`expr $j + 3`
    # echo $i
    # echo $j
    # python meshing_batch.py --layer 6 --chunk_start $i 0 0 --chunk_end $j 44 3 --mip 2 --cg_name h01_full0_v2 --queue_name https://sqs.us-east-2.amazonaws.com/622009480892/cave-meshing-sqs
done