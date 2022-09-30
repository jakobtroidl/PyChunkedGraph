#!/bin/sh
# [504, 349,  21]

max=125
for idx in `seq 0 $max`
do
    i=`expr $idx \* 4`
    j=`expr $j + 4`
    # echo $i
    # echo $j
    python meshing_batch.py --layer 3 --chunk_start $i 0 0 --chunk_end $j 349 21 --mip 2 --cg_name h01_full0_v2 --queue_name https://sqs.us-east-2.amazonaws.com/622009480892/cave-meshing-sqs
done