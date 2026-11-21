#!/bin/sh
set -u
cd /work/project
export MAVEN_CONFIG=/work/home/.m2
mvn -B -ntp -U -s /runner-settings.xml -Dmaven.repo.local=/work/m2 -Dmaven.wagon.http.connectionTimeout=3000 -Dmaven.wagon.http.readTimeout=3000 clean install
build=$?
integration=0
if [ "$RUN_INTEGRATION" = true ]; then
    mvn -B -ntp -U -s /runner-settings.xml -Dmaven.repo.local=/work/m2 -Dmaven.wagon.http.connectionTimeout=3000 -Dmaven.wagon.http.readTimeout=3000 -Pintegration-test failsafe:integration-test failsafe:verify
    integration=$?
fi
printf 'build=%s\nintegration=%s\n' "$build" "$integration" > exit-codes.txt
find . -type f \( -path '*/target/surefire-reports/TEST-*.xml' -o -path '*/target/failsafe-reports/TEST-*.xml' \) -print > /work/report-files.txt
printf 'exit-codes.txt\n' >> /work/report-files.txt
tar -cf /work/results.tar -T /work/report-files.txt
exit 0
