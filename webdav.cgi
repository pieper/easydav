#!/bin/bash

export LC_CTYPE=en_US.UTF-8
python -c '
from flup.server.cgi import WSGIServer
from webdav import main
WSGIServer(main).run()
'    
