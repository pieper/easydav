#!/usr/bin/python
# -*- coding: utf-8 -*-

from flup.server.fcgi import WSGIServer
from webdav import main
WSGIServer(main).run()
    
