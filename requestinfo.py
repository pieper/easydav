# -*- coding: utf-8 -*-

import os.path
import urlparse
import urllib
import wsgiref.util
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError

import davutils
from davutils import DAVError
from lock_manager import LockManager
import webdavconfig as config

class RequestInfo(object):
    '''Parses WSGI environment dictionary and gives easy access to parameters
    that are relevant for WebDAV.
    '''
    def __init__(self, environ):
        self.environ = environ
        self.wsgi_input = environ['wsgi.input']
        self._lockmanager = None
        self.root_url = self.get_root_url()
        self.check_if_header()
    
    def get_lockmanager(self):
        if self._lockmanager is None and config.lock_db:
            self._lockmanager = LockManager()
        return self._lockmanager
    
    lockmanager = property(get_lockmanager)
    
    def get_root_url(self):
        '''Get the url where the webdav resource is located.
        E.g. 'http://server/path/webdav.cgi/'
        '''
        if config.root_url is not None:
            url = config.root_url
        else:
            url = wsgiref.util.guess_scheme(self.environ) # 'http' or 'https'
            url += '://' + self.environ['HTTP_HOST']
            if self.environ.has_key('REQUEST_URI'):
                assert self.environ['REQUEST_URI'].startswith('/')
                url += self.environ['REQUEST_URI']
                path = self.environ.get('PATH_INFO', '')
                
                # Remove the WebDAV relative path from the end of the url
                assert url.endswith(path)
                url = url[:-len(path)]
                                
        # Some programs require the root directory url to include
        # a trailing slash, because otherwise Apache performs a
        # 302 Found redirect to the version with a slash.
        if not url.endswith('/'):
            url += '/'
        
        return url
    
    def check_if_header(self):
        '''Check the If: header to determine whether the request should be
        executed. Fills in self.provided_tokens or raises DAVError('412') if
        conditions are not satisfied.
        '''
        self.provided_tokens = []
        if not self.environ.has_key('HTTP_IF'):
            return True
        
        all_passed = False
        for uri, conditions in davutils.parse_if_header(self.environ['HTTP_IF']):
            if uri is None:
                rel_path = self.parse_request_path()
            else:
                rel_path = self.parse_simple_ref(uri)
            
            for c_type, c_invert, c_value in conditions:
                if c_type == 'etag':
                    real_path = self.get_real_path(rel_path, 'r')
                    cond_passed = (davutils.create_etag(real_path) == c_value)
                elif c_type == 'token':
                    cond_passed = self.lockmanager.validate_lock(rel_path, c_value)
                    self.provided_tokens.append((rel_path, c_value))
                
                if c_invert:
                    cond_passed = not cond_passed
                
                if not cond_passed:
                    break
            else:
                all_passed = True
        
        if not all_passed:
            raise DAVError('412 Precondition Failed: If header')
    
    def assert_read(self, real_path):
        '''Verify that a remote web dav user is allowed to read this path,
        and that the path exists. Throws DAVError otherwise.
        The path can be either a file or a directory.
        '''
        if not davutils.path_inside_directory(real_path, config.root_dir):
            raise DAVError('403 Permission Denied: Path is outside root_dir')
        
        if davutils.compare_path(real_path, config.restrict_access):
            raise DAVError('403 Permission Denied: restrict_access')
        
        if not os.path.exists(real_path):
            raise DAVError('404 Not Found')
        
        if not os.access(real_path, os.R_OK):
            raise DAVError('403 Permission Denied: File mode excludes read')
    
    def assert_write(self, real_path):
        '''Verify that a remote web dav user is allowed to write this path.
        Throws DAVError otherwise.
        
        If the path does not exist, checks that the user is allowed to create
        a directory or a file at this path.
        If the path exists, checks that the file or directory can be overwritten.
        
        If the resource is locked, verifies that the request included a valid
        If:-header with the lock token.
        '''
        if not davutils.path_inside_directory(real_path, config.root_dir):
            raise DAVError('403 Permission Denied: Path is outside root_dir')
        
        if davutils.compare_path(real_path, config.restrict_access):
            raise DAVError('403 Permission Denied: restrict_access')
        
        if davutils.compare_path(real_path, config.restrict_write):
            raise DAVError('403 Permission Denied: restrict_write')
        
        if not os.path.exists(real_path):
            # Check parent directory for permission to create file
            parent_dir = os.path.dirname(real_path)
            if not os.path.isdir(parent_dir):
                raise DAVError('409 Conflict: Parent is not directory')
            
            if not os.access(parent_dir, os.W_OK):
                raise DAVError('403 Permission Denied: Parent mode excludes write')
        else:
            parent_dir = None
            if not os.access(real_path, os.W_OK):
                raise DAVError('403 Permission Denied: File mode excludes write')
        
        self.assert_locks(real_path)
    
    def assert_locks(self, real_path):
        '''Verify that there are no locks on the resource, or that the necessary
        locks have been provided by the client in the If: header.
        '''
        if self.lockmanager:
            rel_path = davutils.get_relpath(real_path, config.root_dir)
            applied_locks = self.lockmanager.get_locks(rel_path, 0)
                
            if not os.path.exists(real_path):
                parent_dir = os.path.dirname(rel_path)
                applied_locks += self.lockmanager.get_locks(parent_dir, 0)
            
            for lock in applied_locks:
                if lock.urn not in [token for path, token in self.provided_tokens]:
                    raise DAVError('423 Locked: ' + lock.path)
    
    def get_real_path(self, rel_path, mode):
        '''Get real filesystem path based on a path relative to repository root.
        Also verifies proper access permissions or raises DAVError.
        '''
        real_path = os.path.join(config.root_dir, rel_path)
        
        if mode == 'w':
            self.assert_write(real_path)
        elif mode == 'r':
            self.assert_read(real_path)
        else:
            raise ValueError('Invalid access mode, must be r or w.')
        
        return real_path
    
    def parse_request_path(self):
        '''Return relative path based on PATH_INFO.'''
        path = urllib.unquote(self.environ.get('PATH_INFO', ''))
        path = unicode(path, 'utf-8').strip('/')
        return path
    
    def get_request_path(self, mode):
        '''Return the real filesystem path based on PATH_INFO from environment,
        and verify access rights.
        '''
        return self.get_real_path(self.parse_request_path(), mode)
    
    def parse_simple_ref(self, simple_ref):
        '''Return a relative path for the given simple_ref reference.
        RFC4918: Simple-ref = absolute-URI | ( path-absolute [ "?" query ] )
        '''
        if simple_ref.startswith('/'):
            url = wsgiref.util.guess_scheme(environ) # 'http' or 'https'
            url += '://' + self.environ['HTTP_HOST']
            url += simple_ref
        else:
            url = simple_ref
        
        if not url.startswith(self.root_url):
            return None
        
        rel_path = url[len(self.root_url):].strip('/')
        return unicode(urllib.unquote(rel_path), 'utf-8')
    
    def get_destination_path(self, mode):
        '''Return the real filesystem path for url given in HTTP Destination
        header and verify access rights.
        '''
        request_url = self.environ.get('HTTP_DESTINATION', '')
        rel_path = self.parse_simple_ref(request_url)
        
        if rel_path is None:
            raise DAVError('502 Bad Gateway')
        
        return self.get_real_path(rel_path, mode)
    
    def get_url(self, real_path):
        '''Get a fully specified URI for the file referenced
        by path. The URL is encoded with % escapes.
        '''
        rel_path = davutils.get_relpath(real_path, config.root_dir)
        
        rel_path = urllib.quote(rel_path.encode('utf-8'))
        url = urlparse.urljoin(self.root_url, rel_path)
        
        if os.path.isdir(real_path) and not url.endswith('/'):
            url += '/' # Trailing slash for directories
        
        return url
    
    def get_depth(self, default = '0'):
        '''Get the Depth: -http header value.
        Returns integer >= 0 or -1 for 'infinity'.
        '''
        depth = self.environ.get('HTTP_DEPTH', default)
        if depth == 'infinity':
            return -1
        
        try:
            return int(depth)
        except ValueError:
            raise DAVError('400 Bad Request: Invalid Depth header')
    
    def get_overwrite(self):
        '''Get the HTTP Overwrite header, returning True if overwrite is allowed.'''
        overwrite = self.environ.get('HTTP_OVERWRITE', 'T').upper()
        if overwrite not in ['T', 'F']:
            raise DAVError('400 Bad Request: Invalid overwrite header')
        return overwrite == 'T'
    
    def check_ifmatch(self, etag):
        '''Parse and check HTTP If-Match and If-None-Match headers
        against the specified ETag.
        If this returns False, the calling function should abort
        with 412 Precondition Failed.
        '''
        if_match = self.environ.get('HTTP_IF_MATCH', '')
        if_none_match = self.environ.get('HTTP_IF_NONE_MATCH', '')
        
        if if_match and if_none_match:
            raise DAVError('400 Bad Request: If-Match conflicts with If-None-Match')
        
        if not os.path.exists(real_path):
            # If-match header fails if there is no current entity.
            return not if_match
        
        if if_match:
            return davutils.compare_etags(etag, if_match)
        else:
            return not davutils.compare_etags(etag, if_none_match)
    
    def get_xml_body(self):
        '''Decode the request body with ElementTree, returning an
        Element object or None.'''
        
        body = self.wsgi_input.read()
        
        if not body.strip():
            return None
        
        try:
            return ET.fromstring(body)
        except ExpatError, e:
            raise DAVError('400 Bad Request: ' + str(e))
    
    def parse_propfind_body(self, allprops):
        '''Parse the XML body for a PROPFIND request.
        
        Returns a list of strings, specifying the requested properties,
        OR the special string 'propname' without any list.
        '''
        
        body = self.get_xml_body()
        
        if not body:
            # Treat empty request body like allprop request
            return allprops
        
        if body.tag != '{DAV:}propfind':
            raise DAVError('400 Bad Request: Root element is not propfind')
        
        if body.find('{DAV:}allprop'):
            include_element = body.find('{DAV:}include')
            if include_element:
                includes = [t.tag for t in include_element.getchildren()]
                return allprops + includes
            else:
                return allprops
        
        if body.find('{DAV:}propname'):
            return 'propname'
        
        prop_element = body.find('{DAV:}prop')
        if not prop_element:
            raise DAVError('400 Bad Request: No prop in propfind')
        
        props = [t.tag for t in prop_element.getchildren()]
        return props
    
    def parse_proppatch(self):
        '''Parse the XML request body for a PROPPATCH request.
        
        Returns a list of instructions, represented as tuples of
        either type:
        - Sets: ('set', propname, propelement)
        - Removes: ('remove', propname, None)
        
        Propelement is a ElementTree element, with tag name same as propname.
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

if __name__ == '__main__':
    print "Unit tests"
    
    import tempfile, shutil
    config.root_dir = tempfile.mkdtemp(prefix = 'easydav-')
    testfile = os.path.join(config.root_dir, 'testfile')
    
    open(testfile, 'w').write('foo')
    
    mgr = LockManager()
    lock = mgr.create_lock('testfile', True, '', -1, 100)
    
    req = RequestInfo({
        'HTTP_HOST': 'example.com',
        'REQUEST_URI': '/webdav.cgi/testfile',
        'PATH_INFO': '/testfile',
        'HTTP_IF': '(<' + lock.urn + '>)',
        'wsgi.input': None
    })
    
    # When holding the lock
    assert req.get_request_path('r')
    assert req.get_request_path('w')
    
    mgr.release_lock(lock.path, lock.urn)
    lock = mgr.create_lock('testfile', True, '', -1, 100)
    
    # When there is another lock
    assert req.get_request_path('r')
    try:
        assert not req.get_request_path('w')
    except DAVError:
        pass
    
    mgr.release_lock(lock.path, lock.urn)
    
    # When there are no locks
    assert req.get_request_path('r')
    assert req.get_request_path('w')
    
    assert req.get_url(testfile) == 'http://example.com/webdav.cgi/testfile'
    assert req.parse_simple_ref(req.get_url(testfile)) == 'testfile'
    
    shutil.rmtree(config.root_dir)
    
    print "Unit tests OK"