#!/bin/bash

# 노드 이름 리스트 (psid00 ~ psid10)
for i in $(seq -w 0 4); do
    NODE="ga0$i"
    echo "===== $NODE ====="
    
    ssh $NODE "
    	echo "9582" | sudo -S pkill -f -9 cp2k
    "
    echo ""
done
