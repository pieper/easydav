EasyDAV - A simple to deploy WSGI webdav implementation.

License
=======

Copyright 2010 Petteri Aimonen <jpa@wd.mail.kapsi.fi>

Redistribution and use in source and binary forms, with or without modification, are
permitted provided that the following conditions are met:

   1. Redistributions of source code must retain the above copyright notice, this list of
      conditions and the following disclaimer.

   2. Redistributions in binary form must reproduce the above copyright notice, this list
      of conditions and the following disclaimer in the documentation and/or other materials
      provided with the distribution.


System Requirements
===================

EasyDAV requires Python 2.5 or newer (but not 3.x), the Kid template library
and flup WSGI library. Flup can also easily be replaced with any other
WSGI-compatible library.

Installation
============

First create a configuration file for the script by copying webdavconfig.py.example
to webdavconfig.py. You must set atleast '''root_dir''' in this file. This is
the filesystem path to the root folder that will contain the files accessible
through WebDAV.

Possible deployment methods are:
 1) A standalone server, using wsgiref package. Mostly for testing purposes.
   
    Just run webdav.py. The server will be on http://localhost:8080/.
    Port and host can be changed in the end of webdav.py.

 2) A normal CGI script under Apache or other webserver.

    Copy the files to a folder under webserver document root, for example
    to ~/public_html/webdav. Copy htaccess_example.txt to .htaccess and
    change the '''RewriteBase''' in this file.

    Alternatively, don't create .htaccess and you can access the directory
    with the url http://domain.com/~user/webdav/webdav.cgi/. In that case
    you might want to take steps to protect webdavconfig.py from access
    through web server, by e.g. setting chmod 700.

 3) An FCGI script under Apache or other webserver.

    Do as in 2), and after verifying functionality, change the script name
    in .htaccess to '''webdav.fcgi'''. Note that when using FCGI, any changes
    you make to webdavconfig.py don't come to effect until you kill the process.

Security
========

You should protect access to the WebDAV repository by using HTTP Authentication,
with for example Apache mod_auth. Most WebDAV clients support HTTPS and Digest
authentication, so use either of them so that passwords are not transmitted
in plain text.

Every step has been taken to protect the script from accessing files outside
'''root_dir'''. The worst the WebDAV users can do is fill up the hard drive.

When the root directory is accessible through web server (like when managing
web pages through WebDAV), users might upload an executable file. The default
configuration prohibits writing to .php, .pl, .cgi and .fcgi files. You should
add any other extensions recognized by your web server.

Known bugs
==========

When using CGI or FCGI interface, litmus warns that:
WARNING: DELETE removed collection resource with Request-URI including fragment;
This is because Apache strips the last part of urls like
http://domain.com/dir/#fragment. I don't know a workaround, and I'm not sure
what harm this causes. Possibly something with strange filenames.

Missing features
================
The server implements WebDAV to level 1. The locking mechanisms in level 2 are
not supported. This probably harms interoperation with Microsoft Office.

The server supports only predefined properties. Custom properties cannot be set
and therefore the litmus testset 'props' fails.

