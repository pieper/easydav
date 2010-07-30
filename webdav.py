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
import urlparse
import urllib
import wsgiref.util
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError
import zipfile

from davutils import *
import webdavconfig as config

multistatus = kid.load_template('multistatus.kid')
dirindex = kid.load_template('dirindex.kid')

def assert_read(real_path):
    '''Verify that a remote web dav user is allowed to read this path,
    and that the path exists. Throws DAVError otherwise.
    The path can be either a file or a directory.
    '''
    if not path_inside_directory(real_path, config.root_dir):
        raise DAVError('403 Permission Denied: Path is outside root_dir')
    
    if not os.path.exists(real_path):
        raise DAVError('404 Not Found')
    
    if not os.access(real_path, os.R_OK):
        raise DAVError('403 Permission Denied: File mode excludes read')
    
    if compare_path(real_path, config.restrict_access):
        raise DAVError('403 Permission Denied: restrict_access')

def assert_write(real_path):
    '''Verify that a remote web dav user is allowed to write this path.
    Throws DAVError otherwise.
    
    If the path does not exist, checks that the user is allowed to create
    a directory or a file at this path.
    If the path exists, checks that the file or directory can be overwritten.
    '''
    if not path_inside_directory(real_path, config.root_dir):
        raise DAVError('403 Permission Denied: Path is outside root_dir')
    
    if not os.path.exists(real_path):
        # Check parent directory for permission to create file
        parent_dir = os.path.dirname(real_path)
        if not os.path.isdir(parent_dir):
            raise DAVError('409 Conflict: Parent is not directory')
        
        if not os.access(parent_dir, os.W_OK):
            raise DAVError('403 Permission Denied: Parent mode excludes write')
    else:
        if not os.access(real_path, os.W_OK):
            raise DAVError('403 Permission Denied: File mode excludes write')

    if compare_path(real_path, config.restrict_access):
        raise DAVError('403 Permission Denied: restrict_access')
    
    if compare_path(real_path, config.restrict_write):
        raise DAVError('403 Permission Denied: restrict_write')

def get_real_path(request_path, mode):
    '''Convert a path relative to repository root to a complete file system
    path and check access permissions. Environment dictionary can be passed
    instead of request_path, in which case this function will first call
    get_path().
    
    Mode is either 'r' or 'w'.
    '''
    assert mode in ['r', 'w']
    
    if isinstance(request_path, dict):
        request_path = get_path(request_path)
    
    real_path = os.path.join(config.root_dir, request_path)
    
    if mode == 'w':
        assert_write(real_path)
    else:
        assert_read(real_path)
    
    return real_path

def get_root_url(environ):
    '''Get the url where the webdav resource is located.
    E.g. 'http://server/path/webdav.cgi/'
    '''
    if config.root_url is not None:
        url = config.root_url
    else:
        url = wsgiref.util.guess_scheme(environ) # 'http' or 'https'
        url += '://' + environ['HTTP_HOST']
        if environ.has_key('REQUEST_URI'):
            url += environ['REQUEST_URI']
            path = urllib.quote(get_path(environ).encode('utf-8'))
            if path:
                # Remove the WebDAV relative path from the end of the url
                url = url.rstrip('/')[:-len(path)]
        else:
            url += '/'
                            
    # Some programs require the root directory url to include
    # a trailing slash, because otherwise Apache performs a
    # 302 Found redirect to the version with a slash.
    if not url.endswith('/'):
        url += '/'
    
    return url

def get_path(environ):
    '''Get the path argument from request line.
    URL escapes, like %20, are decoded.
    Any leading and trailing slashes are removed from result so
    that e.g. os.path.dirname() works correctly.
    '''
    path = unicode(urllib.unquote(environ.get('PATH_INFO', '')), 'utf-8')
    return path.strip('/')

def get_destination(environ):
    '''Get the path argument from Destination header.'''
    request_url = environ.get('HTTP_DESTINATION', '')
    request_url = unicode(urllib.unquote(request_url), 'utf-8')
    my_url = get_root_url(environ)
    
    if not request_url.startswith(my_url):
        raise DAVError('502 Bad Gateway')
    
    return request_url[len(my_url):].strip('/')

def get_real_url(real_path, root_url):
    '''Get a fully specified URI for the file referenced
    by path. The URL is encoded with % escapes.
    '''
    
    # Get path relative to the root directory
    root = os.path.normpath(config.root_dir) + '/'
    real_path_slash = os.path.normpath(real_path) + '/'
    assert real_path_slash[:len(root)] == root
    rel_path = real_path_slash[len(root):]
    
    if not os.path.isdir(real_path):
        rel_path = rel_path.rstrip('/') # No trailing slash for files
    
    rel_path = urllib.quote(rel_path.encode('utf-8'))
    return urlparse.urljoin(root_url, rel_path)

def get_depth(environ, default = 0):
    '''Get the Depth: -http header value.
    Returns integer >= 0 or -1 for 'infinity'.
    '''
    depth = environ.get('HTTP_DEPTH', default)
    if depth == 'infinity':
        return -1
    else:
        try:
            return int(depth)
        except ValueError:
            raise DAVError('400 Bad Request: Invalid Depth header')

def get_length(environ):
    '''Get the length of body data associated with the request.'''
    try:
        return int(environ.get('CONTENT_LENGTH') or '0')
    except ValueError:
        raise DAVError('400 Bad Request: Invalid Content-length header')

def get_overwrite(environ):
    '''Get the HTTP Overwrite header, returning True if overwrite is allowed.'''
    overwrite = environ.get('HTTP_OVERWRITE', 'T')
    if overwrite not in ['T', 'F']:
        raise DAVError('400 Bad Request: Invalid overwrite header')
    return overwrite == 'T'

def get_xml_body(environ):
    '''Decode the request body with ElementTree, returning an
    Element object or None.'''
    body_length = get_length(environ)
    
    if body_length:
        body = environ['wsgi.input'].read(body_length)
        try:
            return ET.fromstring(body)
        except ExpatError, e:
            raise DAVError('400 Bad Request: ' + str(e))
    else:
        return None

def check_ifmatch(environ, real_path):
    '''Parse and check HTTP If-Match and If-None-Match headers
    against the ETag of the specified file.
    If this returns False, the calling function should abort
    with 412 Precondition Failed.
    '''
    if_match = environ.get('HTTP_IF_MATCH', '')
    if_none_match = environ.get('HTTP_IF_NONE_MATCH', '')
    
    if if_match and if_none_match:
        raise DAVError('400 Bad Request: If-Match conflicts with If-None-Match')
    
    if not os.path.exists(real_path):
        # If-match header fails if there is no current entity.
        if if_match:
            return False
        else:
            return True
    
    etag = create_etag(real_path)
    if if_match:
        return compare_etags(etag, if_match)
    else:
        return not compare_etags(etag, if_none_match)

def handle_options(environ, start_response):
    '''Handle an OPTIONS request.'''
    start_response('200 OK', [('DAV', '1')])
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
        lambda path: get_isoformat(os.path.getctime(path)),
        None
    ),
    '{DAV:}getcontentlength': (
        lambda path: str(os.path.getsize(path)),
        None
    ),
    '{DAV:}getetag': (
        create_etag,
        None
    ),
    '{DAV:}getlastmodified': (
        lambda path: get_rfcformat(os.path.getmtime(path)),
        set_mtime
    ),
    '{DAV:}getcontenttype': (
        get_mimetype,
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
            add_to_dict_list(propstats, '404 Not Found: Property', (prop, ''))
        else:
            try:
                value = property_handlers[prop][0](real_path)
                add_to_dict_list(propstats, '200 OK', (prop, value))
            except Exception, e:
                add_to_dict_list(propstats, '500 ' + str(e), (prop, ''))
    
    return propstats

def parse_propfind(environ):
    '''Returns a list of strings, specifying the requested properties,
    OR the special string 'propname' without any list.
    '''
    if not get_length(environ):
        # Treat empty request body like allprop request
        return property_handlers.keys()
    
    body = get_xml_body(environ)
    
    if body.tag != '{DAV:}propfind':
        raise DAVError('400 Bad Request: Root element is not propfind')
    
    if body.find('{DAV:}allprop'):
        include_element = body.find('{DAV:}include')
        if include_element:
            includes = [t.tag for t in include_element.getchildren()]
            return property_handlers.keys() + includes
        else:
            return property_handlers.keys()
    
    if body.find('{DAV:}propname'):
        return 'propname'
    
    prop_element = body.find('{DAV:}prop')
    if not prop_element:
        raise DAVError('400 Bad Request: No prop in propfind')
    
    props = [t.tag for t in prop_element.getchildren()]
    return props

def handle_propfind(environ, start_response):
    request_depth = get_depth(environ)
    request_props = parse_propfind(environ)
    real_path = get_real_path(environ, 'r')
    root_url = get_root_url(environ)
    
    result_files = []
    for path in search_directory(real_path, request_depth):
        try:
            assert_read(path)
        except DAVError:
            continue # Skip forbidden paths from listing
        
        real_url = get_real_url(path, root_url)
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

def parse_proppatch(environ):
    '''Returns a list of instructions, represented as tuples of
    either type:
    - Sets: ('set', propname, propelement)
    - Removes: ('remove', propname, None)
    '''
    body = get_xml_body(environ)
    
    if body.tag != '{DAV:}propertyupdate':
        raise DAVError('400 Bad Request: Root element is not propertyupdate')
    
    instructions = []
    for element in body.getchildren():
        if element.tag == '{DAV:}set':
            if element[0].tag != '{DAV:}prop':
                raise DAVError('400 Bad Request: Non-prop element in DAV:set')
            
            for propelement in element[0]:
                instructions.append(('set', propelement.tag, propelement))
        
        elif element.tag == '{DAV:}remove':
            if element[0].tag != '{DAV:}prop':
                raise DAVError('400 Bad Request: Non-prop element in DAV:remove')
            
            for propelement in element[0]:
                instructions.append(('set', propelement.tag, None))
    
    return instructions

def handle_proppatch(environ, start_response):
    real_path = get_real_path(environ, 'w')
    real_url = get_real_url(real_path, get_root_url(environ))
    
    propstats = {}
    instructions = parse_proppatch(environ)
    
    # Servers MUST process PROPPATCH instructions in
    # document order. Instructions MUST either all be
    # executed or none executed. (RFC4918)
    for instruction in instructions:
        try:
            proppatch_verify_instruction(real_path, instruction)
            add_to_dict_list(propstats, '200 OK', (instruction[1], ''))
        except DAVError, e:
            add_to_dict_list(propstats, e, (instruction[1], ''))
    
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

def handle_put(environ, start_response):
    real_path = get_real_path(environ, 'w')
    byte_count = get_length(environ)
    
    if os.path.isdir(real_path):
        raise DAVError('405 Method Not Allowed: Overwriting directory')
    
    if not check_ifmatch(environ, real_path):
        raise DAVError('412 Precondition Failed')
    
    new_file = not os.path.exists(real_path)
    if not new_file:
        os.unlink(real_path)
    
    outfile = open(real_path, 'wb')
    write_blocks(outfile, read_blocks(environ['wsgi.input'], byte_count))
    
    if new_file:
        start_response('201 Created', [])
    else:
        start_response('204 No Content', [])
    
    return ""

def handle_get(environ, start_response):
    real_path = get_real_path(environ, 'r')
    
    if os.path.isdir(real_path):
        return handle_dirindex(environ, start_response)
    
    if not check_ifmatch(environ, real_path):
        raise DAVError('412 Precondition Failed')
    
    start_response('200 OK',
        [('Content-Type', get_mimetype(real_path)),
         ('E-Tag', create_etag(real_path)),
         ('Content-Length', str(os.path.getsize(real_path)))])
    
    if environ['REQUEST_METHOD'] == 'HEAD':
        return ''
    
    infile = open(real_path, 'rb')
    return read_blocks(infile)

def handle_mkcol(environ, start_response):
    real_path = get_real_path(environ, 'w')
    
    if os.path.exists(real_path):
        raise DAVError('405 Method Not Allowed: Collection already exists')

    if get_length(environ):
        environ['wsgi.input'].read(get_length(environ))
        raise DAVError('415 Unsupported Media Type')
    
    os.mkdir(real_path)
    
    start_response('201 Created', [])
    return ""

def handle_delete(environ, start_response):
    real_path = get_real_path(environ, 'w')
    
    if not os.path.exists(real_path):
        raise DAVError('404 Not Found')
    
    if os.path.isdir(real_path):
        shutil.rmtree(real_path)
    else:
        os.unlink(real_path)
    
    start_response('204 No Content', [])
    return ""

def handle_copy_move(environ, start_response):
    depth = get_depth(environ)
    real_source = get_real_path(get_path(environ), 'r')
    real_dest = get_real_path(get_destination(environ), 'w')
    
    new_resource = not os.path.exists(real_dest)
    if not new_resource:
        if not get_overwrite(environ):
            raise DAVError('412 Precondition Failed: Would overwrite')
        elif os.path.isdir(real_dest):
            shutil.rmtree(real_dest)
        else:
            os.unlink(real_dest)
    
    if environ['REQUEST_METHOD'] == 'COPY':
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


def handle_dirindex(environ, start_response, message = None):
    '''Handle a GET request for a directory.
    Result is unimportant for DAV clients and only ment of WWW browsers.
    '''
    real_path = get_real_path(environ, 'r')
    root_url = get_root_url(environ)
    real_url = get_real_url(real_path, root_url)
    
    # No parent directory link in repository root
    has_parent = (root_url.rstrip('/') != real_url.rstrip('/'))
    
    files = os.listdir(real_path)
    for filename in files:
        try:
            assert_read(os.path.join(real_path, filename))
        except DAVError:
            files.remove(filename) # Remove forbidden files from listing
    
    files.sort(key = lambda f: not os.path.isdir(os.path.join(real_path, f)))
    
    start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
    t = dirindex.Template(
        real_url = real_url, real_path = real_path,
        files = files, has_parent = has_parent,
        message = message, root_url = root_url
    )
    return [t.serialize(output = 'xhtml')]

def handle_post(environ, start_response):
    '''Handle a POST request.
    Used for file uploads and deletes in the HTML GUI.
    '''
    fields = cgi.FieldStorage(fp = environ['wsgi.input'], environ = environ)
    real_path = get_real_path(environ, 'r')
    message = ""
    
    if fields.getfirst('file'):
        f = fields['file']
        dest_path = os.path.join(real_path, f.filename)
        assert_write(dest_path)
        
        if os.path.isdir(dest_path):
            raise DAVError('405 Method Not Allowed: Overwriting directory')
    
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        
        outfile = open(dest_path, 'wb')
        write_blocks(outfile, read_blocks(f.file))
        
        message = "Successfully uploaded " + f.filename + "."
    
    if fields.getfirst('btn_remove'):
        filenames = fields.getlist('select')
        
        for f in filenames:
            rm_path = os.path.join(real_path, f)
            assert_write(rm_path)
            
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
                assert_read(path)
                return True
            except DAVError:
                return False
        
        for f in filenames:
            file_path = os.path.join(real_path, f)
            assert_read(file_path)
            add_to_zip_recursively(zipobj, file_path, config.root_dir, check_read)
        
        zipobj.close()
        
        start_response('200 OK', [
            ('Content-Type', 'application/zip'),
            ('Content-Length', str(datafile.tell()))
        ])
        
        datafile.seek(0)
        return read_blocks(datafile)
    
    return handle_dirindex(environ, start_response, message)

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
            if request_handlers.has_key(request_method):
                return request_handlers[request_method](environ, start_response)  
        except DAVError, e:
            start_response(e.httpstatus, [('Content-Type', 'text/plain')])
            return [e.httpstatus]
        
        if get_length(environ):
            # Apache gives error "(104)Connection reset by peer:
            # ap_content_length_filter: apr_bucket_read() failed" if the script
            # does not read body.
            environ['wsgi.input'].read(get_length(environ))
        
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
