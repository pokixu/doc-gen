#!/usr/bin/env/python3

# Requires the `markdown2` and `toml` packages:
#   `pip install markdown2 toml`
#

# interpreter and general external access
import sys
import os
import os.path
import argparse
import subprocess
from jinja2 import Environment, FileSystemLoader, select_autoescape

# directories manipulation
import shutil
import gzip

# data structures
from pathlib import Path
from typing import List
from collections import defaultdict
from import_name import ImportName
from mathlib_data_structures import mathlibStructures, declaration, tactic, tacticCategories, declarationKindsSource, declarationKindsDestination, generalPages

# data structures manipulation
import json
import toml
import glob
import re
import textwrap
import markdown2
import html
from urllib.parse import quote
from functools import reduce
import networkx as nx
from operator import itemgetter

# get arguments from script inititation
parser = argparse.ArgumentParser('Options to print_docs.py')
parser.add_argument('-w', help = 'Specify site root URL')
parser.add_argument('-l', help = 'Symlink CSS and JS instead of copying', action = "store_true")
parser.add_argument('-r', help = 'relative path to mathlib root directory')
parser.add_argument('-t', help = 'relative path to html output directory')
CLI_ARGS = parser.parse_args()

# setup templates environment
env = Environment(
  loader=FileSystemLoader('templates', 'utf-8'),
  autoescape=select_autoescape(['html', 'xml'])
)

# extra doc files to include in generation
# the content has moved to the community website,
# but we still build them to avoid broken links
# format: (filename_root, display_name, source, community_site_url)
# TODO: avoid broken links and remove unnecessary structures and action
EXTRA_DOC_FILES = [
  ('overview', 'mathlib overview', 'docs/mathlib-overview.md', 'mathlib-overview'),
  ('tactic_writing', 'tactic writing', 'docs/extras/tactic_writing.md', 'extras/tactic_writing'),
  ('calc', 'calc mode', 'docs/extras/calc.md', 'extras/calc'),
  ('conv', 'conv mode', 'docs/extras/conv.md', 'extras/conv'),
  ('simp', 'simplification', 'docs/extras/simp.md', 'extras/simp'),
  ('well_founded_recursion', 'well founded recursion', 'docs/extras/well_founded_recursion.md','extras/well_founded_recursion'),
  ('style', 'style guide', 'docs/contribute/style.md','contribute/style'),
  ('doc_style', 'documentation style guide', 'docs/contribute/doc.md','contribute/doc'),
  ('naming', 'naming conventions', 'docs/contribute/naming.md','contribute/naming')
]

# setup global variables
ROOT = os.getcwd()
SITE_ROOT = CLI_ARGS.w if CLI_ARGS.w else "/"
MATHLIB_SRC_ROOT = os.path.join(ROOT, CLI_ARGS.r if CLI_ARGS.r else '_target/deps/mathlib') + '/'
MATHLIB_DEST_ROOT = os.path.join(ROOT, CLI_ARGS.t if CLI_ARGS.t else 'html', '') # TODO: make sure nothing is left in docs_destination_root
LINK_PATTERNS = [(re.compile(r'Note \[(.*)\]', re.I), SITE_ROOT + r'notes.html#\1')]
LEAN_COMMIT = subprocess.check_output(['lean', '--run', 'src/lean_commit.lean']).decode()

with open('leanpkg.toml') as f:
  parsed_toml = toml.loads(f.read())
ml_data = parsed_toml['dependencies']['mathlib']
MATHLIB_COMMIT = ml_data['rev']
MATHLIB_GITHUB_ROOT = ml_data['git'].strip('/')

# ------------------------------------------------ START utils reading -------------------------------------------------

def separate_results(objs):
  file_map = defaultdict(list)
  loc_map = {}
  for obj in objs:
    # replace the filenames in-place with parsed filename objects
    i_name = obj[declaration.FILENAME] = ImportName.of(obj[declaration.FILENAME])
    if i_name.project == '.':
      continue  # this is doc-gen itself
    
    file_map[i_name].append(obj)
    loc_map[obj[declaration.NAME]] = i_name
    for (cstr_name, _) in obj[declaration.CONSTRUCTORS]:
      loc_map[cstr_name] = i_name
    for (sf_name, _) in obj[declaration.STRUCTURE_FIELDS]:
      loc_map[sf_name] = i_name
    if len(obj[declaration.STRUCTURE_FIELDS]) > 0:
      loc_map[obj[declaration.NAME] + '.mk'] = i_name
  return file_map, loc_map

# ------------------------------------------------ END utils reading ---------------------------------------------------


# ------------------------------------------------ START read source files -------------------------------------------------

def read_src_data():
  with open('export.json', 'r', encoding='utf-8') as f:
    lib_data = json.load(f, strict=False)

  file_map, loc_map = separate_results(lib_data[mathlibStructures.DECLARATIONS])

  for entry in lib_data[mathlibStructures.TACTICS]:
    if len(entry[tactic.TAGS]) == 0:
      entry[tactic.TAGS] = ['untagged']

  mod_docs = {ImportName.of(f): docs for f, docs in lib_data[mathlibStructures.MODULE_DESCRIPTIONS].items()}
  # ensure the key is present for `default.lean` modules with no declarations
  for i_name in mod_docs:
    if i_name.project == '.':
      continue  # this is doc-gen itself
    file_map[i_name]

  return file_map, loc_map, lib_data[mathlibStructures.NOTES], mod_docs, lib_data[mathlibStructures.INSTANCES], lib_data[mathlibStructures.TACTICS]

# ------------------------------------------------ END read source files -----------------------------------------------------

# ------------------------------------------------ START common utils --------------------------------------------------------

def convert_markdown(ds, toc=False):
  extras = ['code-friendly', 'cuddled-lists', 'fenced-code-blocks', 'link-patterns', 'tables']
  if toc:
    extras.append('toc')
  return markdown2.markdown(ds, extras=extras, link_patterns = LINK_PATTERNS)

# returns (pagetitle, intro_block), [(tactic_name, tactic_block)]
def split_tactic_list(markdown):
  entries = re.findall(r'(?<=# )(.*)([\s\S]*?)(?=(##|\Z))', markdown)
  return entries[0], entries[1:]

# ------------------------------------------------ END common utils ----------------------------------------------------------

# ------------------------------------------------ START templates setup ----------------------------------------------------
def trace_deps(file_map):
  graph = nx.DiGraph()
  import_name_by_path = {k.raw_path: k for k in file_map}
  n = 0
  n_ok = 0
  for k in file_map:
    deps = subprocess.check_output(['lean', '--deps', str(k.raw_path)]).decode()
    graph.add_node(k)
    for p in deps.split():
      n += 1
      try:
        p = import_name_by_path[Path(p).with_suffix('.lean')]
      except KeyError:
        print(f"trace_deps: Path does not end in 'lean': {p}")
        continue
      graph.add_edge(k, p)
      n_ok += 1
  print(f"trace_deps: Processed {n_ok} / {n} dependency links")
  return graph

def mk_site_tree(file_map: List[ImportName]):
  filenames = [ [filename.project] + list(filename.parts) for filename in file_map ]
  return mk_site_tree_core(filenames)

def mk_site_tree_core(filenames, path=[]):
  entries = []

  for dirname in sorted(set(dirname for dirname, *rest in filenames if rest != [])):
    new_path = path + [dirname]
    entries.append({
      "kind": "project" if not path else "dir",
      "name": dirname,
      "path": '/'.join(new_path[1:]),
      "children": mk_site_tree_core([rest for dn, *rest in filenames if rest != [] and dn == dirname], new_path)
    })

  for filename in sorted(filename for filename, *rest in filenames if rest == []):
    new_path = path + [filename]
    entries.append({
      "kind": "file",
      "name": filename,
      "path": '/'.join(new_path[1:]) + '.html',
    })

  return entries

def import_options(loc_map, decl_name, import_string):
  direct_import_paths = []
  if decl_name in loc_map:
    direct_import_paths.append(loc_map[decl_name].name)
  if import_string != '' and import_string not in direct_import_paths:
    direct_import_paths.append(import_string)
  if any(i.startswith('init.') for i in direct_import_paths):
    return '<details class="imports"><summary>Import using</summary><ul>{}</ul>'.format('<li>imported by default</li>')
  elif len(direct_import_paths) > 0:
    return '<details class="imports"><summary>Import using</summary><ul>{}</ul>'.format(
      '\n'.join(['<li>import {}</li>'.format(d) for d in direct_import_paths]))
  else:
    return ''

def kind_of_decl(decl):
  if len(decl[declaration.STRUCTURE_FIELDS]) > 0:
    return declarationKindsDestination.STRUCTURE
  elif len(decl[declaration.CONSTRUCTORS]) > 0:
    return declarationKindsDestination.INDUCTIVE
  elif decl[declaration.KIND] == declarationKindsSource.THEOREM: 
    return declarationKindsDestination.THEOREM
  elif decl[declaration.KIND] == declarationKindsSource.CONST:
    return declarationKindsDestination.CONST
  elif decl[declaration.KIND] == declarationKindsSource.AXIOM:
    return declarationKindsDestination.AXIOM

def tag_id_of_name(tag):
  return tag.strip().replace(' ', '-')

def linkify_core(decl_name, text, loc_map):
  if decl_name in loc_map:
    tooltip = ' title="{}"'.format(decl_name) if text != decl_name else ''
    return '<a href="{0}#{1}"{3}>{2}</a>'.format(
      SITE_ROOT + loc_map[decl_name].url, decl_name, text, tooltip)
  elif text != decl_name:
    return '<span title="{0}">{1}</span>'.format(decl_name, text)
  else:
    return text

def linkify_efmt(f, loc_map):
  def linkify_linked(string, loc_map):
    return ''.join(
      match[4] if match[0] == '' else
      match[1] + linkify_core(match[0], match[2], loc_map) + match[3]
      for match in re.findall(r'\ue000(.+?)\ue001(\s*)(.*?)(\s*)\ue002|([^\ue000]+)', string))

  def go(f):
    if isinstance(f, str):
      f = f.replace('\n', ' ')
      # f = f.replace(' ', '&nbsp;')
      return linkify_linked(f, loc_map)
    elif f[0] == 'n':
      return f'<span class="fn">{go(f[1])}</span>'
    elif f[0] == 'c':
      return go(f[1]) + go(f[2])
    else:
      raise Exception('unknown efmt object')

  return go(['n', f])

def linkify_markdown(string, loc_map):
  def linkify_type(string):
    splitstr = re.split(r'([\s\[\]\(\)\{\}])', string)
    tks = map(lambda s: linkify_core(s, s, loc_map), splitstr)
    return "".join(tks)

  string = re.sub(r'<code>([^<]+)</code>',
    lambda p: '<code>{}</code>'.format(linkify_type(p.group(1))), string)
  string = re.sub(r'<span class="n">([^<]+)</span>',
    lambda p: '<span class="n">{}</span>'.format(linkify_type(p.group(1))), string)
  return string

def link_to_decl(decl_name, loc_map):
  return f'{SITE_ROOT}{loc_map[decl_name].url}#{decl_name}'

def plaintext_summary(markdown, max_chars = 200):
  # collapse lines
  text = re.compile(r'([a-zA-Z`(),;\$\-]) *\n *([a-zA-Z`()\$])').sub(r'\1 \2', markdown)

  # adapted from https://github.com/writeas/go-strip-markdown/blob/master/strip.go
  remove_keep_contents_patterns = [
    r'(?m)^([\s\t]*)([\*\-\+]|\d\.)\s+',
    r'\*\*([^*]+)\*\*',
    r'\*([^*]+)\*',
    r'(?m)^\#{1,6}\s*([^#]+)\s*(\#{1,6})?$',
    r'__([^_]+)__',
    r'_([^_]+)_',
    r'\!\[(.*?)\]\s?[\[\(].*?[\]\)]',
    r'\[(.*?)\][\[\(].*?[\]\)]'
  ]
  remove_patterns = [
    r'^\s{0,3}>\s?', r'^={2,}', r'`{3}.*$', r'~~', r'^[=\-]{2,}\s*$',
    r'^-{3,}\s*$', r'^\s*'
  ]

  text = reduce(lambda text, p: re.compile(p, re.MULTILINE).sub(r'\1', text), remove_keep_contents_patterns, text)
  text = reduce(lambda text, p: re.compile(p, re.MULTILINE).sub('', text), remove_patterns, text)

  # collapse lines again
  text = re.compile(r'\s*\.?\n').sub('. ', text)

  return textwrap.shorten(text, width = max_chars, placeholder="…")
  
def htmlify_name(n):
  return '.'.join([f'<span class="name">{ html.escape(part) }</span>' for part in n.split('.')])

def library_link(filename: ImportName, line=None):
  mathlib_github_src_root = "{0}/blob/{1}/src/".format(MATHLIB_GITHUB_ROOT, MATHLIB_COMMIT)
  lean_root = 'https://github.com/leanprover-community/lean/blob/{}/library/'.format(LEAN_COMMIT)

  # TODO: allow extending this for third-party projects
  library_link_roots = {
    'core': lean_root,
    'mathlib': mathlib_github_src_root,
  }

  try:
    root = library_link_roots[filename.project]
  except KeyError:
    return ""  # empty string is handled as a self-link

  root += '/'.join(filename.parts) + '.lean'
  if line is not None:
    root += f'#L{line}'
  return root

def setup_jinja_env(file_map, loc_map, instances):
  env.globals['import_graph'] = trace_deps(file_map)
  env.globals['site_tree'] = mk_site_tree(file_map)
  env.globals['instances'] = instances
  env.globals['import_options'] = lambda d, i: import_options(loc_map, d, i)
  env.globals['find_import_path'] = lambda d: find_import_path(loc_map, d)
  env.globals['sorted'] = sorted
  env.globals['extra_doc_files'] = EXTRA_DOC_FILES
  env.globals['mathlib_github_root'] = MATHLIB_GITHUB_ROOT
  env.globals['mathlib_commit'] = MATHLIB_COMMIT
  env.globals['lean_commit'] = LEAN_COMMIT
  env.globals['site_root'] = SITE_ROOT
  env.globals['kind_of_decl'] = kind_of_decl
  env.globals['tag_id_of_name'] = tag_id_of_name
  env.globals['tag_ids_of_names'] = lambda names: ' '.join(tag_id_of_name(n) for n in names)
  env.globals['library_link'] = library_link

  env.filters['linkify'] = lambda x: linkify_core(x, x, loc_map)
  env.filters['linkify_efmt'] = lambda x: linkify_efmt(x, loc_map)
  env.filters['convert_markdown'] = lambda x: linkify_markdown(convert_markdown(x), loc_map) # TODO: this is probably very broken
  env.filters['link_to_decl'] = lambda x: link_to_decl(x, loc_map)
  env.filters['plaintext_summary'] = plaintext_summary
  env.filters['split_on_hr'] = lambda x: x.split('\n---\n', 1)[-1]
  env.filters['htmlify_name'] = htmlify_name
  env.filters['library_link'] = library_link

# ------------------------------------------------ END templates setup ------------------------------------------------------


# ------------------------------------------------ START write destination files ----------------------------------------------

def open_outfile(filename, mode = 'w'):
  filename = os.path.join(MATHLIB_DEST_ROOT, filename)
  os.makedirs(os.path.dirname(filename), exist_ok=True)
  return open(filename, mode, encoding='utf-8')

def write_pure_md_file(source, dest, name):
  with open(source) as infile:
    body = convert_markdown(infile.read(), True)

  with open_outfile(dest) as out:
    out.write(env.get_template('pure_md.j2').render(
      active_path = '',
      name = name,
      body = body,
    ))

def write_html_files(file_map, notes, mod_docs, tactic_docs):
  with open_outfile('index.html') as out:
    out.write(env.get_template('index.j2').render(
      active_path=''))

  with open_outfile('404.html') as out:
    out.write(env.get_template('404.j2').render(
      active_path=''))

  with open_outfile('notes.html') as out:
    out.write(env.get_template('notes.j2').render(
      active_path='',
      notes = sorted(notes, key = lambda n: n[0])))

  types_of_tactics = [
    (tacticCategories.TACTIC, generalPages.TACTICS), 
    (tacticCategories.COMMAND, generalPages.COMMANDS), 
    (tacticCategories.HOLE_COMMAND, generalPages.HOLE_COMMANDS), 
    (tacticCategories.ATTRIBUTE, generalPages.ATTRIBUTES)
  ]
  for (category, filename) in types_of_tactics:
    entries = [e for e in tactic_docs if e[tactic.CATEGORY] == category]
    with open_outfile(filename + '.html') as out:
      out.write(env.get_template(filename + '.j2').render(
        active_path='',
        entries = sorted(entries, key = lambda n: n[tactic.NAME]),
        tagset = sorted(set(t for e in entries for t in e[tactic.TAGS]))
      ))

  for filename, decls in file_map.items():
    md = mod_docs.get(filename, [])
    with open_outfile(MATHLIB_DEST_ROOT + filename.url) as out:
      out.write(env.get_template('module.j2').render(
        active_path = filename.url,
        filename = filename,
        items = sorted(md + decls, key = lambda d: d[declaration.LINE]),
        decl_names = sorted(d[declaration.NAME] for d in decls),
      ))

  for (filename, displayname, source, _) in EXTRA_DOC_FILES:
    write_pure_md_file(MATHLIB_SRC_ROOT + source, filename + '.html', displayname)

def write_site_map(file_map):
  with open_outfile('sitemap.txt') as out:
    for filename in file_map:
      out.write(SITE_ROOT + filename.url + '\n')
    for name in dir(generalPages):
      if not name.startswith('__'):
        out.write(SITE_ROOT + name + '.html\n')
    for (filename, _, _, _) in EXTRA_DOC_FILES:
      out.write(SITE_ROOT + filename + '.html\n')

def name_in_decl(decl_name, decl):
  if decl[declaration.NAME] == decl_name:
    return True
  if decl_name in [sf[0] for sf in decl[declaration.STRUCTURE_FIELDS]]:
    return True
  if decl_name in [cnstr[0] for cnstr in decl[declaration.CONSTRUCTORS]]:
    return True
  return False

def library_link_from_decl_name(decl_name, decl_loc, file_map):
  try:
    e = next(decl for decl in file_map[decl_loc] if name_in_decl(decl_name, decl))
  except StopIteration as e:
    if decl_name[-3:] == '.mk':
      return library_link_from_decl_name(decl_name[:-3], decl_loc, file_map)
    print(f'{decl_name} appears in {decl_loc}, but we do not have data for that declaration. file_map:')
    print(file_map[decl_loc])
    raise e
  return library_link(decl_loc, e[declaration.LINE])

def write_docs_redirect(decl_name, decl_loc):
  url = SITE_ROOT + decl_loc.url
  with open_outfile('find/' + decl_name + '/index.html') as out:
    out.write(f'<meta http-equiv="refresh" content="0;url={url}#{quote(decl_name)}">')

def write_src_redirect(decl_name, decl_loc, file_map):
  url = library_link_from_decl_name(decl_name, decl_loc, file_map)
  with open_outfile('find/' + decl_name + '/src/index.html') as out:
    out.write(f'<meta http-equiv="refresh" content="0;url={url}">')

# TODO: see if we can do without the 'find' dir
def write_redirects(loc_map, file_map):
  for decl_name in loc_map:
    if decl_name.startswith('con.') and sys.platform == 'win32':
      continue  # can't write these files on windows
    write_docs_redirect(decl_name, loc_map[decl_name])
    write_src_redirect(decl_name, loc_map[decl_name], file_map)

def copy_web_files(path, use_symlinks):
  def cp(a, b):
    if use_symlinks:
      try:
        os.remove(b)
      except FileNotFoundError:
        pass
      os.symlink(os.path.relpath(a, os.path.dirname(b)), b)
    else:
      shutil.copyfile(a, b)

  cp('style.css', path+'style.css')
  cp('pygments.css', path+'pygments.css')
  cp('nav.js', path+'nav.js')
  cp('searchWorker.js', path+'searchWorker.js')

def copy_yaml_files(path):
  for name in ['100.yaml', 'undergrad.yaml', 'overview.yaml']:
    shutil.copyfile(f'{MATHLIB_SRC_ROOT}docs/{name}', path+name)

def copy_static_files(path):
  for filename in glob.glob(os.path.join(ROOT, 'static', '*.*')):
    shutil.copy(filename, path)

def copy_files(path, use_symlinks): 
  copy_web_files(path, use_symlinks)
  copy_yaml_files(path)
  copy_static_files(path)
  
# write only declaration names in separate file
# TODO: delete when using data structure for searching
def write_decl_txt(loc_map):
  with open_outfile('decl.txt') as out:
    out.write('\n'.join(loc_map.keys()))

def mk_export_map_entry(decl_name, filename, kind, is_meta, line):
  return {
    'filename': str(filename.raw_path),
    'kind': kind,
    'is_meta': is_meta,
    'line': line,
    'src_link': library_link(filename, line),
    'docs_link': f'{SITE_ROOT}{filename.url}#{decl_name}'
  }

def mk_export_db(file_map):
  export_db = {}
  for decls in file_map.values():
    for obj in decls:
      name, filename, kind, is_meta, line, constructors, structure_fields = itemgetter(
        declaration.NAME, 
        declaration.FILENAME, 
        declaration.KIND, 
        declaration.IS_META, 
        declaration.LINE,
        declaration.CONSTRUCTORS,
        declaration.STRUCTURE_FIELDS
      )(obj)
      export_db[name] = mk_export_map_entry(name, filename, kind, is_meta, line)
      export_db[name]['decl_header_html'] = env.get_template('decl_header.j2').render(decl=obj)
      for (cstr_name, _) in constructors:
        export_db[cstr_name] = mk_export_map_entry(cstr_name, filename, kind, is_meta, line)
      for (sf_name, _) in structure_fields:
        export_db[sf_name] = mk_export_map_entry(sf_name, filename,  kind, is_meta, line)
  return export_db

def write_export_db(export_db):
  json_str = json.dumps(export_db)
  with gzip.GzipFile(MATHLIB_DEST_ROOT + 'export_db.json.gz', 'w') as zout:
    zout.write(json_str.encode('utf-8'))

def mk_export_searchable_map_entry(filename_name, name, description, kind = '', attributes = []):
  return {
    'module': filename_name,
    'name': name,
    'description': description,
    'kind': kind,
    'attributes': attributes,
  }

def mk_export_searchable_db(file_map, tactic_docs):
  export_searchable_db = []

  for fn, decls in file_map.items():
    filename_name = str(fn.url)
    for obj in decls:
      decl_entry = mk_export_searchable_map_entry(filename_name, obj['name'], obj['doc_string'], obj['kind'], obj['attributes'])
      export_searchable_db.append(decl_entry)
      for (cstr_name, _) in obj['constructors']:
        cstr_entry = mk_export_searchable_map_entry(filename_name, cstr_name, obj['doc_string'], obj['kind'], obj['attributes'])
        export_searchable_db.append(cstr_entry)
      for (sf_name, _) in obj['structure_fields']:
        sf_entry = mk_export_searchable_map_entry(filename_name, sf_name, obj['doc_string'], obj['kind'], obj['attributes'])
        export_searchable_db.append(sf_entry)

  for tactic in tactic_docs:
    # category is the singular form of each docs webpage in 'General documentation'
    # e.g. 'tactic' -> 'tactics.html'
    tactic_entry_container_name = f"{tactic['category']}s.html"
    tactic_entry = mk_export_searchable_map_entry(tactic_entry_container_name, tactic['name'], tactic['description'])
    export_searchable_db.append(tactic_entry)

  return export_searchable_db

def write_export_searchable_db(searchable_data):
  json_str = json.dumps(searchable_data)
  with open_outfile('searchable_data.json') as out:
    out.write(json_str)

def main():
  file_map, loc_map, notes, mod_docs, instances, tactic_docs = read_src_data()
  setup_jinja_env(file_map, loc_map, instances)
  write_html_files(file_map, notes, mod_docs, tactic_docs)
  write_decl_txt(loc_map)
  write_redirects(loc_map, file_map)
  copy_files(MATHLIB_DEST_ROOT, use_symlinks=CLI_ARGS.l)
  write_export_db(mk_export_db(file_map))
  write_export_searchable_db(mk_export_searchable_db(file_map, tactic_docs))
  write_site_map(file_map)

if __name__ == '__main__':
  main()