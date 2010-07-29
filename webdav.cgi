#!/usr/bin/python
# -*- coding: utf-8 -*-

from flup.server.cgi import WSGIServer
from webdav import main
WSGIServer(main).run()
    
