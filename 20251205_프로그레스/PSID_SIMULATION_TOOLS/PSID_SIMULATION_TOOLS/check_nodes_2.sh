# 노드 이름 리스트 (psid00 ~ psid10)
for i in $(seq -w 0 4); do
    NODE="ga0$i"
    echo "===== $NODE ====="

    ssh $NODE "
        TOTAL_CORES=\$(nproc)
        LOAD_AVG=\$(uptime | awk -F'load average:' '{print \$2}' | cut -d',' -f1 | xargs)
        echo \"Total CPUs     : \$TOTAL_CORES\"
        echo \"Load Avg (1min): \$LOAD_AVG\"
        USED_CORES=\$(printf \"%.0f\" \$(echo \"\$LOAD_AVG\" | bc))
        echo \"Used CPUs est. : \$USED_CORES\"
    "
    echo ""
done

