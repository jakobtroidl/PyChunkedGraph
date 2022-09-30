#!/bin/sh

max=251
for idx in `seq 0 $max`
do
    i=`expr $idx \* 4`
    j=`expr $j + 4`
    python meshing_batch.py --layer 2 --chunk_start $i 0 0 --chunk_end $j 697 42 --mip 2 --cg_name h01_full0_v2 --queue_name https://sqs.us-east-2.amazonaws.com/622009480892/cave-meshing-sqs
done