
#!/bin/bash

echo "Checking SkillPulse health..."

kubectl get pods -n skillpulse-prd

echo ""
echo "Checking backend API..."

kubectl port-forward -n skillpulse-prd service/backend 18080:8080 >/dev/null 2>&1 &
PID=$!

trap 'kill "$PID" 2>/dev/null' EXIT

sleep 3

if curl -fsS --max-time 5 http://localhost:18080/health; then
    echo ""
    echo "SkillPulse backend is healthy!"
else
    echo ""
    echo "SkillPulse health check FAILED!"
    exit 1
fi
