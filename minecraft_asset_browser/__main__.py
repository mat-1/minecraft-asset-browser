from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape
import aiohttp
from datetime import datetime, timedelta
import zipfile
import io
import time
import asyncio

routes = web.RouteTableDef()

jinja_env = Environment(
	loader=FileSystemLoader(searchpath='templates'),
	autoescape=select_autoescape(['html', 'xml']),
	enable_async=True,
	trim_blocks=True,
	lstrip_blocks=True
)

def get_name_from_object_url(object_url):
	return '/'.join(object_url.rsplit('/', 2)[1:])

def gettime(t, exact=False):
	if isinstance(t, str):
		t = datetime.fromisoformat(t)
	if isinstance(t, (datetime, timedelta)):
		t = (datetime.now() - t.replace(tzinfo=None)).total_seconds()
	if exact:
		output = []
		h = int(t // (60 * 60))
		m = int(t // 60) % 60
		s = int(t) % 60
		ms = int(t * 1000) % 1000
		if h == 1: output.append(f'1 hour')
		elif h: output.append(f'{h} hours')
		if m == 1: output.append(f'1 minute')
		elif m: output.append(f'{m} minutes')
		if s == 1: output.append(f'1 second')
		elif s: output.append(f'{s} seconds')
		if ms == 1: output.append(f'1 millisecond')
		elif ms: output.append(f'{ms} milliseconds')
		return ', '.join(output)
	t = int(t)

	if t <= 1:
		return 'A second'
	if t <= 2:
		return 'A couple seconds'
	elif t <= 10:
		return 'A few seconds'
	elif t < 60:
		return 'Less than a minute'
	elif t < 60 * 2:
		return f'a minute'
	elif t < 60 * 60:
		return f'{t // 60} minutes'
	elif t < 60 * 60 * 2:
		return f'an hour'
	elif t < 60 * 60 * 60:
		return f'{t // (60 * 60)} hours'
	elif t < 60 * 60 * 60 * 2:
		return f'{t // (60 * 60 * 60)} day'
	elif t < 60 * 60 * 60 * 24:
		return f'{t // (60 * 60 * 60)} days'
	elif t < 60 * 60 * 24 * 30 * 2:
		return f'{t // (60 * 60 * 24 * 30)} month'
	elif t < 60 * 60  * 24 * 365:
		return f'{t // (60 * 60  * 24 * 30)} months'
	return f'{t // (60 * 60 * 24 * 365)} years'

def timeago(t):
	return gettime(t) + ' ago'

jinja_env.globals['timeago'] = timeago
jinja_env.globals['get_name_from_object_url'] = get_name_from_object_url
jinja_env.globals['removelastitem'] = lambda l: l[:-1]

class templates:
	template_dict = {}

class Template():
	def __init__(self, filename, **kwargs):
		self.filename = filename
		self.args = kwargs

	async def load_template(self):
		filename, args = self.filename, self.args
		if filename in templates.template_dict:
			t = templates.template_dict[filename]
		else:
			t = jinja_env.get_template(filename)
			templates.template_dict[filename] = t
		r = await t.render_async(**args)
		return r

	async def get_response(self):
		rendered = await self.load_template()
		r = web.Response(
			text=rendered,
			content_type=self.args.get('content_type', 'text/html')
		)
		return r

@web.middleware
async def main_middleware(request, handler):
	try:
		resp = await handler(request)
		if isinstance(resp, Template):
			resp = await resp.get_response()
	except web.HTTPError as e:
		raise e
	return resp

cached_version_id_to_url = {}

async def fetch_versions_json(s: aiohttp.ClientSession):
	async with s.get('https://launchermeta.mojang.com/mc/game/version_manifest.json') as r:
		return await r.json()

async def fetch_version_url(version_id: str, s: aiohttp.ClientSession):
	global cached_version_id_to_url
	if version_id in cached_version_id_to_url:
		return cached_version_id_to_url[version_id]
	version_manifest = await fetch_versions_json(s)
	versions = version_manifest['versions']
	for version in versions:
		if version['id'] == version_id:
			cached_version_id_to_url[version_id] = version['url']
			return version['url']
	else:
		return

async def fetch_version_json(version_id: str, s: aiohttp.ClientSession):
	version_url = await fetch_version_url(version_id, s)
	if version_url is None:
		raise web.HTTPNotFound()
	async with s.get(version_url) as r:
		return await r.json()

async def get_jar_files(url: str, s: aiohttp.ClientSession, search_path: str='/', show_classfiles: bool=False):
	global cached_zipfiles
	search_path = search_path.rstrip('/')

	if (
		url in cached_zipfiles
		and time.time() - cached_zipfiles[url]['last_updated'] < 60 * 5
	):
		jar_zip = cached_zipfiles[url]['zipfile']
	else:
		async with s.get(url) as r:
			jar_bytes = await r.read()
		jar_zip = zipfile.ZipFile(io.BytesIO(jar_bytes), 'r')
		cached_zipfiles[url] = {
			'zipfile': jar_zip,
			'last_updated': time.time()
		}

	found = []
	added_folder_names = set()
	for file in jar_zip.infolist():
		filename = file.filename
		if '/' in filename:
			filepath, name = filename.rsplit('/', 1)
		else:
			filepath, name = '', filename
		if not show_classfiles and filename.endswith('.class'):	
			continue
		filepath = ('/' + filepath).rstrip('/')
		if filepath == search_path:
			found.append({
				'fullpath': filename,
				'name': name,
				'path': filepath
			})
		elif filepath.startswith(search_path):
			filepath = filepath[len(search_path):].strip('/').split('/')[0]
			if filepath and filepath not in added_folder_names:
				added_folder_names.add(filepath)
				found.append({
					'fullpath': search_path.lstrip('/') + '/' + filepath,
					'name': filepath,
					'path': filepath
				})
	found_folders = list(sorted((filedata for filedata in found if '.' not in filedata['name']), key=lambda f: f['name']))
	found_files = list(sorted((filedata for filedata in found if '.' in filedata['name']), key=lambda f: f['name']))
	return found_folders + found_files

cached_zipfiles = {}
package_objects_cache = {}

async def open_jar_file(url, open_filename):
	global cached_zipfiles
	if (
		url in cached_zipfiles
		and time.time() - cached_zipfiles[url]['last_updated'] < 60 * 5
	):
		jar_zip = cached_zipfiles[url]['zipfile']
	else:
		async with s.get(url) as r:
			jar_bytes = await r.read()
		try:
			jar_zip = zipfile.ZipFile(io.BytesIO(jar_bytes), 'r')
		except zipfile.BadZipFile:
			return jar_bytes
		cached_zipfiles[url] = {
			'zipfile': jar_zip,
			'last_updated': time.time()
		}
	with jar_zip.open(open_filename, 'r') as f:
		data = f.read()
		return data


@routes.get('/versions')
async def versions(request):
	s = aiohttp.ClientSession()
	data = await fetch_versions_json(s)
	return Template('versions.html', data=data)

@routes.get('/versions/{version}')
async def view_version(request):
	s = aiohttp.ClientSession()
	version_id = request.match_info['version']
	data = await fetch_version_json(version_id, s)
	return Template('version.html', data=data)

@routes.get('/packages/{hash}/{name}')
async def view_packages_redirect(request):
	hash = request.match_info['hash']
	name = request.match_info['name']
	return web.HTTPFound(f'/packages/{hash}/{name}/')


@routes.get('/versions/{version}/packages/{dir:.*}')
async def view_packages(request):
	version_id = request.match_info['version']
	directory = request.match_info['dir']
	if directory != '/' and directory.endswith('/'):
		raise web.HTTPFound(location=f'/versions/{version_id}/packages/' + directory.strip('/'))

	s = aiohttp.ClientSession()
	version_json = await fetch_version_json(version_id, s)
	assetindex_url = version_json['assetIndex']['url']
	assetindex_id = version_json['assetIndex']['id']
	if (
		assetindex_url in package_objects_cache
		and time.time() - package_objects_cache[assetindex_url]['last_updated'] < 60
	):
		objects = package_objects_cache[assetindex_url]['objects']
	else:
		async with s.get(assetindex_url) as r:
			data = await r.json()
		objects = data['objects']
		package_objects_cache[assetindex_url] = {
			'last_updated': time.time(),
			'objects': objects
		}
	if directory in objects:
		object_data = objects[directory]
		ext = directory.split('.')[-1]
		mimetype = mimetypes.get(ext, 'text/plain')

		object_hash = object_data['hash']
		object_hash_start = object_hash[:2]
		object_url = f'https://resources.download.minecraft.net/{object_hash_start}/{object_hash}'
		async with s.get(object_url) as r:
			content = await r.read()
		
		return web.Response(
			body=content,
			content_type=mimetype,
			charset='utf8'
		)
	else:
		showing_filenames = []
		added_filenames = set()
		for filename in objects:
			if filename.startswith(directory + '/') or not directory:
				filename_after = filename[len(directory):].lstrip('/')
				dirname = filename_after.split('/')[0]
				# if directory:
				# 	directory = directory + '/'
				if dirname not in added_filenames:
					showing_filenames.append({
						'dirname': dirname,
						'path': f'{directory}/{dirname}',
						'fullpath': f'/versions/{version_id}/packages/{directory}/{dirname}'
					})
					added_filenames.add(dirname)
		return Template(
			'assets_list.html',
			path=directory,
			name=assetindex_id,
			filenames=showing_filenames,
			version_id=version_id
		)


@routes.get('/versions/{version}/downloads/{name}/{dir:.*}')
async def view_packages(request):
	version_id = request.match_info['version']
	name = request.match_info['name']
	directory = request.match_info['dir']
	s = aiohttp.ClientSession()
	version_json = await fetch_version_json(version_id, s)
	package_url = version_json['downloads'][name]['url']
	show_class_files = request.query.get('class', 'false').lower() == 'true'

	if package_url.endswith('.jar'):
		if '.' in directory:
			ext = directory.rsplit('.', 1)[-1]
		else:
			ext = ''
		if ext == '':
			jar_files = await get_jar_files(package_url, s, '/' + directory, show_class_files)
			return Template(
				'jar_index.html',
				version_id=version_id,
				files=jar_files,
				jarname=name,
				path=directory,
				show_class_files=show_class_files,
				jar_url=package_url
			)
	else:
		return web.HTTPFound(package_url)
		async with s.get(package_url) as r:
			data = await r.read()
		ext = name.split('.')[-1]

	mimetype = mimetypes.get(ext, 'text/plain')
	data = await open_jar_file(package_url, directory)
	if ext == 'class':
		# data_hash = hashlib.md5(data).hexdigest()
		# data = b'File hash: ' + data_hash.encode() + b'\n\n' + data
		mimetype = 'application/octet-stream'

	return web.Response(
		body=data,
		content_type=mimetype,
		charset='utf8',
		headers={
			'Access-Control-Allow-Origin': '*',
			'cache-control': 'max-age=86400'
		}
	)



mimetypes = {
	'png': 'image/png',
	'json': 'application/json',
	'ogg': 'audio/ogg',
	'txt': 'text/plain',
	'mus': 'application/vnd.musician'
}



@routes.get('/')
async def index(request):
	return web.HTTPFound('/versions')


async def clear_caches():
	while True:
		for zipurl in dict(cached_zipfiles):
			ziptime = cached_zipfiles[zipurl]['last_updated']
			if time.time() - ziptime > 60 * 10:
				del cached_zipfiles[zipurl]
		for assetindexurl in dict(package_objects_cache):
			cachetime = package_objects_cache[assetindexurl]['last_updated']
			if time.time() - cachetime > 60 * 10:
				del package_objects_cache[assetindexurl]
		await asyncio.sleep(60)

loop = asyncio.get_event_loop()

loop.create_task(clear_caches())
app = web.Application(middlewares=[main_middleware])
app.add_routes(routes)
web.run_app(app, loop=loop, port=10573)
