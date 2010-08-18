#!/usr/bin/python
# -*- coding: utf-8 -*-

'''A simple to deploy WSGI webdav implementation.

Copyright 2010 Petteri Aimonen <jpa@wd.mail.kapsi.fi>

Redistribution and use in source and binary forms, with or without modification, are
permitted provided that the following conditions are met:

   1. Redistributions of source code must retain the above copyright notice, this list of
      conditions and the following disclaimer.

   2. Redistributions in binary form must reproduce the above copyright notice, this list
      of conditions and the following disclaimer in the documentation and/or other materials
      provided with the distribution.
'''

__program_name__ = 'EasyDAV'
__version__ = "0.2-dev"

import cgi
import kid
import kid.parser
import os
import os.path
import shutil
import sys
import tempfile
import zipfile

import davutils
from davutils import DAVError
from requestinfo import RequestInfo
import webdavconfig as config

multistatus = kid.load_template('multistatus.kid')
dirindex = kid.load_template('dirindex.kid')

def handle_options(reqinfo, start_response):
    '''Handle an OPTIONS request.'''
    start_response('200 OK', [('DAV', '1,2')])
    return ""

def get_resourcetype(path):
    '''Return the contents for <DAV:resourcetype> property.'''
    if os.path.isdir(path):
        element = kid.parser.Element('{DAV:}collection')
        return kid.parser.ElementStream([
            (kid.parser.START, element),
            (kid.parser.END, element)
        ])
    else:
        return ''

# All supported properties.
# Key is the element name inside DAV:prop element.
# Value is tuple of functions: (get, set)
# Get takes a file name and returns string.
# Set takes a file name and a string value.
# Set may be None to specify protected property.
property_handlers = {
    '{DAV:}creationdate': (
        lambda path: davutils.get_isoformat(os.path.getctime(path)),
        None
    ),
    '{DAV:}getcontentlength': (
        lambda path: str(os.path.getsize(path)),
        None
    ),
    '{DAV:}getetag': (
        davutils.create_etag,
        None
    ),
    '{DAV:}getlastmodified': (
        lambda path: davutils.get_rfcformat(os.path.getmtime(path)),
        davutils.set_mtime
    ),
    '{DAV:}getcontenttype': (
        davutils.get_mimetype,
        None
    ),
    '{DAV:}resourcetype': (
        get_resourcetype,
        None
    )
}

def read_properties(real_path, requested):
    '''Return a propstats dictionary for the file specified by real_path.
    The argument 'requested' is either a list of property names,
    or the special value 'propname'.
    In the second case this function returns all defined properties but no
    values.
    '''
    propstats = {}
    
    if requested == 'propname':
        propstats['200 OK'] = []
        for propname in property_handlers.keys():
            propstats['200 OK'].append((propname, ''))
        return propstats
    
    for prop in requested:
        if not property_handlers.has_key(prop):
            davutils.add_to_dict_list(propstats, '404 Not Found: Property', (prop, ''))
        else:
            try:
                value = property_handlers[prop][0](real_path)
                davutils.add_to_dict_list(propstats, '200 OK', (prop, value))
            except Exception, e:
                davutils.add_to_dict_list(propstats, '500 ' + str(e), (prop, ''))
    
    return propstats

def handle_propfind(reqinfo, start_response):
    depth = reqinfo.get_depth('infinity')
    request_props = reqinfo.parse_propfind_body(property_handlers.keys())
    real_path = reqinfo.get_request_path('r')
    
    result_files = []
    for path in davutils.search_directory(real_path, depth):
        try:
            reqinfo.assert_read(path)
        except DAVError, e:
            if e.httpstatus.startswith('403'):
                continue # Skip forbidden paths from listing
        
        real_url = reqinfo.get_url(path)
        propstats = read_properties(path, request_props)
        result_files.append((real_url, propstats))

    start_response('207 Multistatus',
        [('Content-Type', 'text/xml; charset=utf-8')])
    t = multistatus.Template(result_files = result_files)
    return [t.serialize(output = 'xml')]
     
def proppatch_verify_instruction(real_path, instruction):
    '''Verify that the property can be set on the file,
    or throw a DAVError.
    '''
    command, propname, propelement = instruction
    
    if command == 'set':
        if propelement.getchildren():
            raise DAVError('409 Conflict: XML property values are not supported')
        
        if not property_handlers.has_key(propname):
            raise DAVError('403 Forbidden: No such property')
        
        if property_handlers[propname][1] is None:
            raise DAVError('403 Forbidden',
                '<DAV:cannot-modify-protected-property/>')
    
    elif command == 'remove':
        # No properties to remove so far.
        raise DAVError('403 Forbidden: Properties cannot be removed')

def handle_proppatch(reqinfo, start_response):
    real_path = reqinfo.get_request_path('w')
    real_url = reqinfo.get_url(real_path)
    
    propstats = {}
    instructions = reqinfo.parse_proppatch()
    
    # Servers MUST process PROPPATCH instructions in
    # document order. Instructions MUST either all be
    # executed or none executed. (RFC4918)
    for instruction in instructions:
        try:
            proppatch_verify_instruction(real_path, instruction)
            davutils.add_to_dict_list(propstats, '200 OK', (instruction[1], ''))
        except DAVError, e:
            davutils.add_to_dict_list(propstats, e, (instruction[1], ''))
    
    if propstats.keys() != ['200 OK']:
        if propstats.has_key('200 OK'):
            propstats['424 Failed Dependency'] = propstats['200 OK']
            del propstats['200 OK']
    else:
        for command, propname, propelement in instructions:
            property_handlers[propname][1](real_path, propelement.text)
    
    start_response('207 Multistatus',
        [('Content-Type', 'text/xml; charset=utf-8')])
    t = multistatus.Template(result_files = [(real_url, propstats)])
    return [t.serialize(output = 'xml')]

def handle_put(reqinfo, start_response):
    real_path = reqinfo.get_request_path('w')
    
    if os.path.isdir(real_path):
        raise DAVError('405 Method Not Allowed: Overwriting directory')
    
    if os.path.exists(real_path):
        etag = davutils.create_etag(real_path)
    else:
        etag = None
    
    if not reqinfo.check_ifmatch(etag):
        raise DAVError('412 Precondition Failed')
    
    new_file = not os.path.exists(real_path)
    if not new_file:
        # Unlink the old file to reset mode bits.
        # This has the additional benefit that old GET operations can
        # continue even if the file is replaced.
        os.unlink(real_path)
    
    outfile = open(real_path, 'wb')
    davutils.write_blocks(outfile, davutils.read_blocks(reqinfo.wsgi_input))
    
    if new_file:
        start_response('201 Created', [])
    else:
        start_response('204 No Content', [])
    
    return ""

def handle_get(reqinfo, start_response):
    real_path = reqinfo.get_request_path('r')
    
    if os.path.isdir(real_path):
        return handle_dirindex(reqinfo, start_response)
    
    etag = davutils.create_etag(real_path)
    if not reqinfo.check_ifmatch(etag):
        raise DAVError('412 Precondition Failed')
    
    start_response('200 OK',
        [('Content-Type', davutils.get_mimetype(real_path)),
         ('E-Tag', etag),
         ('Content-Length', str(os.path.getsize(real_path)))])
    
    if reqinfo.environ['REQUEST_METHOD'] == 'HEAD':
        return ''
    
    infile = open(real_path, 'rb')
    return davutils.read_blocks(infile)

def handle_mkcol(reqinfo, start_response):
    real_path = reqinfo.get_request_path('w')
    
    if os.path.exists(real_path):
        raise DAVError('405 Method Not Allowed: Collection already exists')

    if reqinfo.wsgi_input.read():
        raise DAVError('415 Unsupported Media Type')
    
    os.mkdir(real_path)
    
    start_response('201 Created', [])
    return ""

def handle_delete(reqinfo, start_response):
    real_path = reqinfo.get_request_path('w')
    
    # Locks on parent directory prohibit deletion of members.
    reqinfo.assert_locks(os.path.dirname(real_path))
    
    if not os.path.exists(real_path):
        raise DAVError('404 Not Found')
    
    if os.path.isdir(real_path):
        shutil.rmtree(real_path)
    else:
        os.unlink(real_path)
    
    start_response('204 No Content', [])
    return ""

def handle_copy_move(reqinfo, start_response):
    depth = reqinfo.get_depth()
    real_source = reqinfo.get_request_path('r')
    real_dest = reqinfo.get_destination_path('w')
    
    new_resource = not os.path.exists(real_dest)
    if not new_resource:
        if not reqinfo.get_overwrite():
            raise DAVError('412 Precondition Failed: Would overwrite')
        elif os.path.isdir(real_dest):
            shutil.rmtree(real_dest)
        else:
            os.unlink(real_dest)
    
    if reqinfo.environ['REQUEST_METHOD'] == 'COPY':
        if os.path.isdir(real_source):
            if depth == 0:
                os.mkdir(real_dest)
                shutil.copystat(real_source, real_dest)
            else:
                shutil.copytree(real_source, real_dest, symlinks = True)
        else:
            shutil.copy2(real_source, real_dest)
    else:
        shutil.move(real_source, real_dest)
    
    if new_resource:
        start_response('201 Created', [])
    else:
        start_response('204 No Content', [])
    return ""    


def handle_dirindex(reqinfo, start_response, message = None):
    '''Handle a GET request for a directory.
    Result is unimportant for DAV clients and only ment of WWW browsers.
    '''
    real_path = reqinfo.get_request_path('r')
    real_url = reqinfo.get_url(real_path)
    
    # No parent directory link in repository root
    has_parent = (reqinfo.root_url.rstrip('/') != real_url.rstrip('/'))
    
    files = os.listdir(real_path)
    for filename in files:
        try:
            reqinfo.assert_read(os.path.join(real_path, filename))
        except DAVError, e:
            if e.httpstatus.startswith('403'):
                files.remove(filename) # Remove forbidden files from listing
    
    files.sort(key = lambda f: not os.path.isdir(os.path.join(real_path, f)))
    
    start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
    t = dirindex.Template(
        real_url = real_url, real_path = real_path,
        files = files, has_parent = has_parent,
        message = message, root_url = reqinfo.root_url
    )
    return [t.serialize(output = 'xhtml')]

def handle_post(reqinfo, start_response):
    '''Handle a POST request.
    Used for file uploads and deletes in the HTML GUI.
    '''
    fields = cgi.FieldStorage(fp = reqinfo.wsgi_input, environ = reqinfo.environ)
    real_path = reqinfo.get_request_path('r')
    message = ""
    
    if fields.getfirst('file'):
        f = fields['file']
        dest_path = os.path.join(real_path, f.filename)
        reqinfo.assert_write(dest_path)
        
        if os.path.isdir(dest_path):
            raise DAVError('405 Method Not Allowed: Overwriting directory')
    
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        
        outfile = open(dest_path, 'wb')
        davutils.write_blocks(outfile, davutils.read_blocks(f.file))
        
        message = "Successfully uploaded " + f.filename + "."
    
    if fields.getfirst('btn_remove'):
        filenames = fields.getlist('select')
        
        for f in filenames:
            rm_path = os.path.join(real_path, f)
            reqinfo.assert_write(rm_path)
            
            if os.path.isdir(rm_path):
                shutil.rmtree(rm_path)
            else:
                os.unlink(rm_path)
        
        message = "Successfully removed " + str(len(filenames)) + " files."
    
    if fields.getfirst('btn_download'):
        filenames = fields.getlist('select')
        datafile = tempfile.TemporaryFile()
        zipobj = zipfile.ZipFile(datafile, 'w', zipfile.ZIP_DEFLATED, True)
        
        def check_read(path):
            try:
                reqinfo.assert_read(path)
                return True
            except DAVError:
                return False
        
        for f in filenames:
            file_path = os.path.join(real_path, f)
            reqinfo.assert_read(file_path)
            davutils.add_to_zip_recursively(zipobj, file_path,
                config.root_dir, check_read)
        
        zipobj.close()
        
        start_response('200 OK', [
            ('Content-Type', 'application/zip'),
            ('Content-Length', str(datafile.tell()))
        ])
        
        datafile.seek(0)
        return davutils.read_blocks(datafile)
    
    return handle_dirindex(reqinfo, start_response, message)

request_handlers = {
    'OPTIONS': handle_options,
    'PROPFIND': handle_propfind,
    'PROPPATCH': handle_proppatch,
    'GET': handle_get,
    'HEAD': handle_get,
    'PUT': handle_put,
    'MKCOL': handle_mkcol,
    'DELETE': handle_delete,
    'COPY': handle_copy_move,
    'MOVE': handle_copy_move,
    'POST': handle_post,
}

def main(environ, start_response):
    try:
        request_method = environ.get('REQUEST_METHOD', '').upper()
        
        if environ.get('HTTP_EXPECT') and __name__ == '__main__':
            # Expect should work with fcgi etc., but not with the simple_server
            # that is used for testing.
            start_response('400 Bad Request: Expect not supported', [])
            return ""
        
        try:
            reqinfo = RequestInfo(environ)
            if request_handlers.has_key(request_method):
                return request_handlers[request_method](reqinfo, start_response)  
        except DAVError, e:
            start_response(e.httpstatus, [('Content-Type', 'text/plain')])
            return [e.httpstatus]
        
        # Apache gives error "(104)Connection reset by peer:
        # ap_content_length_filter: apr_bucket_read() failed" if the script
        # does not read body.
        environ['wsgi.input'].read()
        
        start_response('501 Not implemented', [('Content-Type', 'text/plain')])
        return ["501 Not implemented"]
    except:
        import traceback
        
        exc = traceback.format_exc()
        if sys.stderr.isatty():
            sys.stderr.write(exc)
        
        try:
            start_response('500 Internal Server Error',
                [('Content-Type', 'text/plain')])
        except AssertionError:
            # Ignore duplicate start_response
            pass
        
        return [exc]

if __name__ == '__main__':
    from wsgiref.simple_server import make_server
    server = make_server('localhost', 8080, main)
    server.serve_forever()
