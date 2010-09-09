#!/bin/bash

status=0

function run_test() 
{
	echo "----------------------------------"
	echo "Testing $1"
	python $1
	if [[ $? != 0 ]]
		then echo "TEST FAILED!"
		status=1
	fi
}

for file in $(grep -il 'unit test' *.py)
	do run_test $file
done

exit $status
