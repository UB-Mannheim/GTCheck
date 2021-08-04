#!/usr/bin/env python3
import datetime
import imghdr
import json
import logging
import os
import re
import shutil
import time
import webbrowser
from collections import defaultdict
from configparser import NoSectionError
from hashlib import sha256, sha1
from logging import Formatter
from logging.handlers import RotatingFileHandler
from pathlib import Path
from subprocess import check_output

import click
import markdown
from flask import Flask, render_template, request, Markup, flash
from git import Repo, InvalidGitRepositoryError

from config import URL, PORT, LOG_DIR, DATA_DIR, SYMLINK_DIR

app = Flask('run') #app = Flask(__name__) throws error at the moment


def modifications(difftext):
    """
    Extract the original and the modified characters as tuples into a list.
    This information is used e.g. for the commit-message.
    :param difftext:
    :return:
    """
    mods = []
    last_pos = 1
    for mod in re.finditer(r'(\[-(.*?)-\]|{\+(.*?)\+})', difftext):
        sub = mod[2] if mod[2] is not None else ""
        add = mod[3] if mod[3] is not None else ""
        if add != "" and len(mods) > 0 and last_pos == mod.regs[0][0]:
            if mods[len(mods) - 1][1] == "":
                mods[len(mods) - 1][1] = add
                continue
        last_pos = mod.regs[0][1]
        mods.append([sub, add])
    return mods


def color_diffs(difftext):
    """
    Adds html-tags to colorize the modified parts.
    :param difftext: Compared text, differences are marked with {+ ADD +} [- DEL -]
    :return:
    """
    return difftext.replace('{+', '<span style="color:green">') \
        .replace('+}', '</span>') \
        .replace('[-', '<span style="color:red">') \
        .replace('-]', '</span>')

def get_diffs(difftext):
    """
    Returns textpassage which differ from origintext
    :param difftext:
    :return:
    """
    origdiff = ' '.join([orig.split('-]')[0] for orig in difftext.split('[-') if '-]' in orig])
    moddiff = ' '.join([mod.split('+}')[0] for mod in difftext.split('{+') if '+}' in mod])
    return origdiff, moddiff


def surrounding_images(img, regex):
    """
    Finding predecessor and successor images to gain more context for the user.
    The basic regex to extract the pagenumber can be set on the setup page and
    is kept in the repo_data['regexnum'] variable (Default  ^(.*?)(\d+)(\D*)$).
    :param img: Imagename
    :return:
    """
    imgmatch = re.match(rf"{regex}", img.name)
    imgint = int(imgmatch[2])
    imgprefix = img.name[:imgmatch.regs[1][1]]
    imgpostfix = img.name[imgmatch.regs[3][0]:]
    prev_img = img.parent.joinpath(
        imgprefix + f"{imgint - 1:0{imgmatch.regs[2][1] - imgmatch.regs[2][0]}d}" + imgpostfix)
    post_img = img.parent.joinpath(
        imgprefix + f"{imgint + 1:0{imgmatch.regs[2][1] - imgmatch.regs[2][0]}d}" + imgpostfix)
    if not prev_img.exists():
        app.logger.info(f"File:{prev_img.name} not found!")
        prev_img = ""
    if not post_img.exists():
        app.logger.info(f"File:{post_img.name} not found!")
        post_img = ""
    return prev_img, post_img


def get_gitdifftext(orig, diff, repo):
    """
    Compares two strings via git hash-objects
    :param orig: Original string
    :param diff: Modified string
    :param repo: repo instance
    :return:
    """
    item_a = sha1((f'blob {len(orig)}\0{orig}').encode('utf-8'))
    item_b = sha1((f'blob {len(diff)}\0{diff}').encode('utf-8'))
    return check_output(['git', 'diff', '-p', '--word-diff', str(item_a.hexdigest()), str(item_b.hexdigest())]).decode(
        'utf-8').split('@@')[-1].strip()


def get_difftext(origtext, item, folder, repo):
    """
    Compares the original and a modified string
    :param origtext: original text string
    :param item: git-python item instances
    :param folder: repo folder
    :param repo: repo instance
    :return:
    """
    # The "<<<<<<< HEAD" indicates a merge conflicts and need other operations
    if "<<<<<<< HEAD\n" in origtext:
        with open(folder.joinpath(item.a_path), 'r') as fin:
            mergetext = fin.read().split("<<<<<<< HEAD\n")[-1].split("\n>>>>>>>")[0].split("\n=======\n")
        difftext = get_gitdifftext(mergetext[0], mergetext[1], repo)
    else:
        try:
            difftext = "".join(item.diff.decode('utf-8').split("\n")[1:])
        except UnicodeDecodeError as ex:
            # The UnicodeDecodeError mostly appears if the orignal character is an combination of unicode symbols
            # e.g. ä -> e+diacritic_mark and the modified character only differs in one and not all parts e.g. ö.
            app.logger.warning(f"File:{item.a_path} Warning the diff text could not be decoded! Error:{ex}")
            try:
                difftext = get_gitdifftext(origtext, item.b_blob.data_stream.read().decode(), repo)
            except Exception as ex2:
                app.logger.warning(f"File:{item.a_path} Both files could not be compared! Error:{ex2}")
                difftext = ""
    return difftext


@app.route('/gtcheck/edit/<group_name>/<repo_path_hash>', methods=['GET', 'POST'])
def gtcheck(group_name, repo_path_hash, repo=None, repo_data=None):
    """
    Gathers the information to render the gtcheck
    :return:
    """
    repo_data_path = get_repo_data_path(group_name, repo_path_hash)
    if repo_data is None:
        repo_data = get_repo_data(repo_data_path)
    if repo is None:
        repo = get_repo(repo_data.get('path'))
    repo_path = Path(repo_data.get('path'))
    name, email = get_git_credentials(repo)
    # Diff Head
    diff_head = repo.git.diff('--cached', '--shortstat').strip().split(" ")[0]
    if not repo_data.get('diff_list') or len(repo_data.get('diff_list')) <= repo_data.get('diff_skipped'):
        repo_data['diff_list'] = [item.a_path for item in repo.index.diff(None) if ".gt.txt" in item.a_path]
    diff_list = repo_data.get('diff_list')[repo_data.get('diff_skipped', 0):]
    nextcounter = 0
    for fileidx, filename in enumerate(diff_list):
        item = repo.index.diff(None, paths=[filename], create_patch=True, word_diff_regex='.')
        if not item and repo_data.get('add_all'):
            path = filename
            origtext = ""
            difftext = open(Path(repo.working_dir).joinpath(filename),'r').read()
            modtext = difftext
            repo_data['modtype'] = "new"
            diffcolored = "<span style='color:green'>The file gets added " \
                              "when committed and deleted when stashed!</span>"
        else:
            if item:
                item = item[0]
            else:
                item = repo.index.diff(None, paths=[filename])[0]
            if not item.a_blob and not item.b_blob:
                pop_idx(repo_data, 'diff_list', repo_data.get('diff_skipped') + fileidx)
                nextcounter += 1
                continue
            repo_data['modtype'] = "mod"
            try:
                origtext = item.a_blob.data_stream.read().decode('utf-8').lstrip(" ")
                path = item.a_path
            except:
                origtext = ""
                path = item.b_path
            difftext = get_difftext(origtext, item, repo_path, repo)
            diffcolored = color_diffs(difftext)
            mergetext = []
            if origtext == "" and not item.deleted_file or item.new_file:
                repo_data['modtype'] = "new"
                diffcolored = "<span style='color:green'>This untracked file gets added " \
                              "when committed and deleted when stashed!</span>"
            if item.deleted_file or not item.b_path:
                repo_data['modtype'] = "del"
                modtext = ""
                diffcolored = "<span style='color:red'>This file gets deleted " \
                              "when committed and restored when stashed!</span>"
            elif mergetext:
                repo_data['modtype'] = "merge"
                modtext = mergetext[1]
            else:
                modtext = repo_path.absolute().joinpath(item.b_path).open().read().lstrip(" ")
            if origtext.strip() == modtext.strip() and origtext.strip() != "" and repo_data.get('skipcc'):
                nextcounter += 1
                if repo_data.get('addcc'):
                    pop_idx(repo_data, 'diff_list', repo_data.get('diff_skipped') + fileidx)
                    repo.git.add(str(filename), u=True)
                else:
                    repo_data['diff_skipped'] += 1
                continue

        # Apply filter options
        if repo_data.get('filter_all')+repo_data.get('filter_from')+repo_data.get('filter_to') != '':
            if repo_data.get('filter_all') != '':
                if not (re.search(rf"{repo_data.get('filter_all')}", origtext) or
                        re.search(rf"{repo_data.get('filter_all')}", modtext)):
                    nextcounter += 1
                    continue
            if repo_data.get('filter_from')+repo_data.get('filter_to') != '':
                origdiff, moddiff = get_diffs(difftext)
                if not (re.search(rf"{repo_data.get('filter_from')}", origdiff) and
                        re.search(rf"{repo_data.get('filter_to')}", moddiff)):
                    nextcounter += 1
                    continue

        fname = repo_path.joinpath(path)
        mods = modifications(difftext)
        if diff_head:
            commitmsg = f"[GT Checked] Staged Files: {diff_head}"
        else:
            commitmsg = f"[GT Checked]  {path}: {', '.join([orig + ' -> ' + mod for orig, mod in mods])}"
        repo_data['modtext'] = modtext
        repo_data['fname'] = str(fname)
        repo_data['fpath'] = str(path)
        repo_data['fileidx'] = fileidx-nextcounter
        custom_keys = [' '.join(repo_data.get('custom_keys')[i:i + 10]) for i in range(0, len(repo_data.get('custom_keys')), 10)]
        inames = [iname for iname in fname.parent.glob(f"{fname.name.replace('gt.txt', '')}*") if imghdr.what(iname)]
        img = inames[0] if inames else None
        if not img:
            write_repo_data(repo_data_path, repo_data)
            return render_template("gtcheck.html", repo_data=repo_data, repo_path_hash=repo_path_hash, group_name=group_name,
                                   branch=repo.active_branch, name=name,
                                   email=email, commitmsg=commitmsg,
                                   difftext=Markup(diffcolored), origtext=origtext, modtext=modtext,
                                   files_left=str(len(repo_data.get('diff_list')) - repo_data.get('diff_skipped')),
                                   iname="No image", fname=str(fname.name), skipped=repo_data.get('diff_skipped'),
                                   vkeylang=repo_data.get('vkeylang'), custom_keys=custom_keys)
        else:
            img_out = Path(SYMLINK_DIR).joinpath(repo_path_hash).joinpath(str(img.relative_to(repo_path)))
            prev_img, post_img = surrounding_images(img_out, repo_data.get('regexnum'))
            write_repo_data(repo_data_path, repo_data)
            return render_template("gtcheck.html", repo_data=repo_data, repo_path_hash=repo_path_hash, group_name=group_name,
                                   branch=repo.active_branch, name=name,
                                   email=email, commitmsg=commitmsg,
                                   image=str(Path(img_out).relative_to(Path(SYMLINK_DIR).parent)),
                                   previmage=str(Path(prev_img).relative_to(Path(
                                       SYMLINK_DIR).parent)) if prev_img != "" else "",
                                   postimage=str(Path(post_img).relative_to(Path(
                                       SYMLINK_DIR).parent)) if post_img != "" else "",
                                   difftext=Markup(diffcolored), origtext=origtext, modtext=modtext,
                                   files_left=str(len(repo_data.get('diff_list')) - repo_data.get('diff_skipped')),
                                   iname=str(img.name), fname=str(fname.name), skipped=repo_data.get('diff_skipped'),
                                   vkeylang=repo_data.get('vkeylang'), custom_keys=custom_keys)
    else:
        if diff_head:
            commitmsg = f"[GT Checked] Staged Files: {diff_head}"
            modtext = f"Please commit the staged files! You skipped {repo_data.get('diff_skipped')} files."
            write_repo_data(repo_data_path, repo_data)
            return render_template("gtcheck.html", repo_data=repo_data, repo_path_hash=repo_path_hash, group_name=group_name,
                                   name=name, email=email, commitmsg=commitmsg, modtext=modtext, custom_keys='', files_left="0")
        if not repo_data.get('diff_list'):
            write_repo_data(repo_data_path, repo_data)
            return render_template("nofile.html")
        repo_data['diff_skipped'] = 0
        write_repo_data(repo_data_path, repo_data)
        return gtcheck(group_name, repo_path_hash, repo, repo_data)


def pop_idx(repo_data, lname, popidx):
    """
    Pops the item from the index off a list, if the index is in the range
    :param lname: Name of the list
    :param popidx: Index to pop
    :return:
    """
    if len(repo_data.get(lname)) > popidx:
        repo_data[lname].pop(popidx)
    return


def set_git_credentials(repo, username, email, level='repository'):
    """ Set the git credentials name and email adress."""
    try:
        if Path(repo.git_dir).joinpath('config.lock').exists():
            Path(repo.git_dir).joinpath('config.lock').unlink()
        repo.config_writer().set_value(level, 'name', username).release()
        repo.config_writer().set_value(level, 'email', email).release()
    except:
        pass


def get_git_credentials(repo, level='repository'):
    """ Return the git credentials name and email adress."""
    username, email = "", ""
    try:
        username = repo.config_reader().get_value(level, 'name')
        email = repo.config_reader().get_value(level, 'email')
    except NoSectionError:
        pass
    return username, email


@app.route('/gtcheck/edit/update/<group_name>/<repo_path_hash>', methods=['GET', 'POST'])
def edit(group_name, repo_path_hash):
    """
    Process the user input from gtcheck html pages
    :return:
    """
    data = request.form  # .to_dict(flat=False)
    repo_data_path = get_repo_data_path(group_name, repo_path_hash)
    repo_data = get_repo_data(repo_data_path)
    repo = get_repo(repo_data.get('path'))
    # Check if mod files left
    difflen = len(repo_data.get('diff_list'))
    repo_data['last_action'] = f"{datetime.date.today()}"
    if difflen <= repo_data.get('diff_skipped'):
         if data['selection'] == 'commit':
             repo.git.commit('-m', data['commitmsg'])
         repo_data['diff_skipped'] = 0
         write_repo_data(repo_data_path, repo_data)
         return gtcheck(group_name, repo_path_hash, repo, repo_data)
    fname = Path(repo_data.get('path')).joinpath(repo_data.get('fpath'))
    modtext = data['modtext'].replace("\r\n", "\n")
    repo_data['vkeylang'] = data['vkeylang']
    repo_data['custom_keys'] = data['custom_keys'].split(' ')
    if data.get('undo', None):
        repo.git.reset('HEAD', repo_data.get('undo_fpath'))
        with open(repo_data.get('undo_fpath'), "w") as fout:
            fout.write(repo_data.get('undo_value'))
            repo_data['diff_skipped'] += 1
    repo_data['undo_fpath'] = str(fname)
    repo_data['undo_value'] = repo_data.get('modtext')
    if data['selection'] == 'commit':
        if difflen - repo_data.get('diff_skipped') != 0:
            if repo_data.get('modtext').replace("\r\n", "\n") != modtext or repo_data.get('modtype') == "merge":
                with open(fname, 'w') as fout:
                    fout.write(modtext)
            repo.git.add(str(fname), u=True)
        repo.git.commit('-m', data['commitmsg'])
    elif data['selection'] == 'stash':
        if repo_data.get('modtype') in ['new']:
            repo.git.rm('-f', str(fname))
        else:
            repo.git.checkout('--', str(fname))
        repo_data['diff_overall'] -= 1
    elif data['selection'] == 'add':
        if repo_data.get('modtext').replace("\r\n", "\n") != modtext or repo_data.get('modtype') == "merge":
            with open(fname, 'w') as fout:
                fout.write(modtext)
        repo.git.add(str(fname), u=True)
    else:
        repo_data['diff_skipped'] += 1
        if data.get('continue_skipped', None):
            repo_data['diff_skipped'] = 0
        write_repo_data(repo_data_path, repo_data)
        return gtcheck(group_name, repo_path_hash, repo, repo_data)
    pop_idx(repo_data, 'diff_list', repo_data.get('diff_skipped') + repo_data.get('fileidx'))
    if data.get('continue_skipped', None):
        repo_data['diff_skipped'] = 0
    write_repo_data(repo_data_path, repo_data)
    return gtcheck(group_name, repo_path_hash, repo, repo_data)


@app.route('/gtcheck/init/<group_name>/<repo_path_hash>', methods=['POST'])
def init(group_name, repo_path_hash):
    """
    Process user input from setup page.
    Initial set the session-variables, which are stored in a cookie.
    Triggers first render of gtcheck html page
    :return:
    """
    data = request.form  # .to_dict(flat=False)
    repo_path = data.get('repo_path', '')
    repo = get_repo(repo_path)
    repo_data_path = get_repo_data_path(group_name, repo_path_hash)
    set_git_credentials(repo, data.get('username', 'GTChecker'), data.get('email', ''))
    logger(str(Path(LOG_DIR).joinpath(f"{repo_path_hash}_{repo.active_branch}.log".replace(' ', '_')).resolve()))
    update_repo_data(repo_data_path, {'username': data.get('username', 'GTChecker'),
                                      'email':  data.get('email', ''),
                                      'addcc': True if 'addCC' in data.keys() else False,
                                      'skipcc': True if 'skipCC' in data.keys() else False,
                                      'custom_keys': data.get('custom_keys', ['']).split(' '),
                                      'regexnum': data.get('regexnum', ''),
                                      'filter_all': data.get('filter_all', ''),
                                      'filter_from': data.get('filter_from', ''),
                                      'filter_to': data.get('filter_to', '')})
    if data.get('reset', 'off') == 'on':
        repo.git.reset()
    if data.get('checkout', 'off') == 'on' and data.get('new_branch', '') != "":
        repo.git.checkout(b=data.get('new_branch', 'main'))
    elif data.get('branches', 'main') != repo.active_branch.name:
        app.logger.warning(f"Branch was force checkout from {str(repo.active_branch.name)} "
                           f"to {data.get('branches', 'main')}")
        repo.git.reset()
        repo.git.checkout('-f', data.get('branches', 'main'))
    return gtcheck(group_name, repo_path_hash, repo)


def purge_folder(path, create_gitkeep=False):
    """
    Purge a folder (with content)
    """
    path = Path(path)
    if path.exists():
        shutil.rmtree(str(path.resolve()), ignore_errors=True)
    path.mkdir()
    if create_gitkeep:
        path.joinpath('.gitkeep').touch()
    return


def clean_symlinks(folder=None):
    """
    Unlink symbolic linked folder in static/symlink
    :return:
    """
    symlinkfolder = Path(__file__).resolve().parent.joinpath(f"static/symlink/")
    for symfolder in symlinkfolder.iterdir():
        if symfolder.is_dir():
            if folder is None:
                symfolder.unlink()
            elif symfolder.name == folder:
                symfolder.unlink()
    return


def create_symlink(folder, symlink_fname):
    symfolder = Path(SYMLINK_DIR).joinpath(symlink_fname)
    # Create symlink to imagefolder
    if not symfolder.exists():
        symfolder.symlink_to(folder)
    return


@app.route('/readme', methods=['GET', 'POST'])
def readme():
    """Show readme file from gt repository"""
    readme_file = request.args.get('readme_file', '')
    with open(readme_file, "r") as fin:
        md_template_string = markdown.markdown(
            fin.read(), extensions=["fenced_code"])
    return md_template_string


@app.route('/gtcheck/setup/<group_name>/<repo_path_hash>', methods=['GET', 'POST'])
def setup(group_name, repo_path_hash):
    """
    Renders setup page
    :return:
    """
    data = request.form  # .to_dict(flat=False)
    repo = get_repo(data['repo_path'])
    repo_data_path = get_repo_data_path(group_name, repo_path_hash)
    repo_data = get_repo_data(repo_data_path)
    username, email = get_git_credentials(repo)
    if data.get('reserve', None):
        reserved_by = data.get('reserved_by', '') if len(data.get('reserved_by', '')) != 0 else "GTChecker"
        update_repo_data(repo_data_path, {'reserved_since': f"{datetime.date.today()}", 'reserved_by': reserved_by})
    if data.get('reservation_cancel', None):
        update_repo_data(repo_data_path, {'reserved_since': '', 'reserved_by': ''})
        return index()
    elif data.get('squash', None):
        repo.git.reset('--soft', repo_data.get('init_head'))
        difflist = [item.a_path for item in repo.index.diff(None) if ".gt.txt" in item.a_path]
        repo.git.add(*difflist, u=True)
        modtext = "The merged commits till start of checking the ground truth will be squashed into one."
        return render_template("gtcheck.html", repo_data=repo_data, repo_path_hash=repo_path_hash,
                               group_name=group_name,
                               name=repo_data.get('username'), email=repo_data.get('email'),
                               commitmsg=f'[GT Checked] Added Files: {len(difflist)}', modtext=modtext, custom_keys='',
                               files_left="0")
    elif data.get('done', None):
        Path(DATA_DIR).joinpath(group_name).joinpath(repo_path_hash + ".json").unlink()
        return index()
    diff_head = repo.git.diff('--cached', '--shortstat').strip().split(" ")[0]
    if diff_head != "":
        flash(
            f"You have {diff_head} staged file[s] in the {repo.active_branch.name} branch! "
            f"These files will be added to the next commit.")
    return render_template("setup.html", username=username, email=email,
                           repo_path=data.get('repo_path', ''), group_name=group_name,
                           repo_path_hash=repo_path_hash,
                           active_branch=repo.active_branch.name,
                           branches=[branch.name for branch in repo.branches] if repo.branches != [] else [repo.active_branch.name],
                           regexnum=repo_data.get('regexnum', "^(.*?)(\d+)(\D*)$"),
                           custom_keys=' '.join(repo_data.get('custom_keys', [''])),
                           filter_all=repo_data.get('filter_all', ''),
                           filter_from=repo_data.get('filter_from', ''),
                           filter_to=repo_data.get('filter_to', ''))


@app.route("/", methods=['GET'])
def index():
    """
    Renders setup page
    :return:
    """
    if app.config['SINGLE']:
        repo = get_repo(app.config['REPO_PATH'])
        username, email = get_git_credentials(repo)
        return render_template("setup.html", username=username, email=email, repo_path=app.config['repo_path'],
                               group_name="Single", repo_path_hash=hash_it(repo.working_dir),
                                active_branch=repo.active_branch, branches=repo.branches,
                               regexnum="^(.*?)(\d+)(\D*)$", custom_keys="",
                               filter_all="", filter_from="", filter_to="")
    else:
        return render_template("index.html", grprepos=get_repo_data_paths())


@app.errorhandler(500)
def internal_error(error):
    """
    Log 500 errors
    :param error:
    :return:
    """
    app.logger.error(str(error))


@app.errorhandler(404)
def not_found_error(error):
    """
    Log 404 errors
    :param error:
    :return:
    """
    app.logger.error(str(error))


def logger(fname):
    """
    Adds rotatingfilehandler to app logger
    :param fname: log filename
    :return:
    """
    file_handler = RotatingFileHandler(fname, maxBytes=100000, backupCount=1)
    file_handler.setFormatter(Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.WARNING)
    if len(app.logger.handlers) > 1:
        app.logger.removeHandler(app.logger.handlers[1])
    app.logger.addHandler(file_handler)


def get_repo(path):
    """
    Returns repo instance, if the subdirectory is provided it goes up to the base directory
    :param path: Repopath
    :return:
    """
    repo = None
    try:
       repo = Repo(path, search_parent_directories=True)
    except InvalidGitRepositoryError:
        app.logger.warning(f'Invalid gitrepository access: {path}')
        pass
    return repo


def get_repo_data_paths():
    repo_data_paths = defaultdict(dict)
    for repo_data_path in Path(DATA_DIR).rglob("*.json"):
        with open(repo_data_path, 'r') as fin:
            repo_data = json.load(fin)
        repo_data_paths[repo_data_path.parent.name][repo_data_path.stem] = repo_data
    return repo_data_paths


def get_repo_data_path(group_name, repo_path_hash):
    repo_group_dir = Path(DATA_DIR).joinpath(group_name)
    return repo_group_dir.joinpath(repo_path_hash + ".json")


def get_repo_data(repo_data_path):
    with open(repo_data_path, 'r') as fin:
        repo_data = json.load(fin)
    return repo_data


def write_repo_data(repo_data_path, repo_data):
    with open(repo_data_path, 'w') as fout:
        json.dump(repo_data, fout, indent=4)


def update_repo_data(repo_data_path, key_vals):
    repo_data = get_repo_data(repo_data_path)
    for key, val in key_vals.items():
        repo_data[key] = val
    write_repo_data(repo_data_path, repo_data)


def add_repo_path(add_all, group_name, set_name, repo_paths, info, readme):
    repogroup_dir = Path(DATA_DIR).joinpath(group_name)
    if not repogroup_dir.exists():
        repogroup_dir.mkdir()
    for repo_path in repo_paths:
        repo = get_repo(repo_path)
        if repo:
            # Add untracked files to index (--intent-to-add)
            [repo.git.add('-N', item) for item in repo.untracked_files if ".gt.txt" in item]
            # Check requirements
            if repo.bare:
                app.logger.error(f'Bare repos can not be added: {repo_path}')
                return
            if not add_all:
                if not repo.is_dirty():
                    app.logger.error(f'The repo contains no modified GT data: {repo_path}')
                    return
                diff_list = [item.a_path for item in repo.index.diff(None) if ".gt.txt" in item.a_path]
                if not diff_list:
                    app.logger.error(f'The repo contains no modified GT data: {repo_path}')
                    return
            else:
                diff_list = [str(path.relative_to(repo.working_dir)) for path in
                             Path(repo.working_dir).rglob('*gt.txt')]

            # Add credentials to repository level
            username, email = get_git_credentials(repo, level='repository' if app.config['WEB'] else 'user')
            set_git_credentials(repo, username, email)
            repo_path = repo.working_dir
            repo_path_hash = hash_it(repo_path)
            repo_data_path = repogroup_dir.joinpath(repo_path_hash+".json")
            create_symlink(repo_path, repo_path_hash)
            if not repo_data_path.exists():
                if readme is None:
                    readmes = [mdfile for mdfile in Path(repo_path).rglob('*.md') if "readme" in mdfile.name.lower()]
                    readme_path = "" if not readmes else str(readmes[0].resolve())
                else:
                    readme_path = str(Path(readme).resolve())
                try:
                    init_head = repo.head.commit.hexsha
                except ValueError:
                    app.logger.warning(f'The repo contains no head commit: {repo_path}')
                    init_head = ""
                infotext = info
                with open(repo_data_path,'w') as fout:
                    json.dump({'path': repo_path,
                               'name': set_name,
                               'info': infotext,
                               'init_head': init_head,
                               'add_all': add_all,
                               'readme': readme_path,
                               'reserved_by': '',
                               'reserved_since': '',
                               'last_action': '',
                               'username': '',
                               'email': '',
                               'addcc': False,
                               'skipcc': True,
                               'regexnum': "^(.*?)(\d+)(\D*)$",
                               'filter_all': '',
                               'filter_from': '',
                               'filter_to': '',
                               'diff_list': diff_list,
                               'diff_overall': len(diff_list),
                               'diff_skipped': 0,
                               'vkeylang': '',
                               'custom_keys': [],
                               'modtext': '',
                               'fname': '',
                               'fpath': '',
                               'fileidx': 0,
                               'undo_fpath': '',
                               'undo_value': '',
                               }, fout, indent=4)


def hash_it(string):
    return sha256(string.encode('utf-8')).hexdigest()


@click.command()
@click.argument('repo-paths', nargs=-1, type=click.Path(exists=True))
@click.option('-a', '--add-all', default=False, is_flag=True, help='Add all ground truth files to the check.')
@click.option('-g', '--group-name', default="default", help='Set the gitrepo to a group')
@click.option('-s', '--set-name', default="", help='Name of the set (default: Set path)')
@click.option('-i', '--info', default="", help="Information to the GT")
@click.option('--readme', nargs=1, type=click.Path(exists=True),
              help="Add readme markdown file from gt repo manually "
                   "(default: add automatically the readme file from the main gitfolder.)")
@click.option('-w', '--web', default=False, is_flag=True, help='Start web version')
@click.option('-s', '--single', default=False, is_flag=True, help='Skip GT selection')
@click.option('-p', '--purge',  multiple=True, type=click.Choice(['symlinks','logs','repodata','all']),
                                                                              help='Purge selection')
def run(repo_paths, add_all, group_name, set_name, info, readme, web, single, purge):
    """
    Starting point to run the app
    :return:
    """
    # Purge the selection
    if 'all' in purge: purge = ['all']
    for purge_sel in purge:
        if purge_sel in ['symlinks', 'all']:
            purge_folder(SYMLINK_DIR, create_gitkeep=True)
        if purge_sel in ['logs', 'all']:
            purge_folder(LOG_DIR, create_gitkeep=True)
        if purge_sel in ['repodata', 'all']:
            purge_folder(DATA_DIR, create_gitkeep=True)
    port = int(os.environ.get('PORT', int(PORT)))
    # Init basic logger
    app.logger.setLevel(logging.INFO)
    if not app.debug:
        logdir = Path(LOG_DIR)
        if not logdir.exists():
            logdir.mkdir()
        logger(str(logdir.joinpath('app.log').resolve()))
    # Set current time as secret_key for the cookie
    # The cookie can keep variables for the whole session (max. 4kb)
    app.config['SECRET_KEY'] = str(int(time.time()))
    app.config['WEB'] = web
    app.config['SINGLE'] = single
    if repo_paths:
        if single:
            app.config['REPO_PATH'] = repo_paths[0]
            add_repo_path(add_all, group_name, set_name, app.config['REPO_PATH'],  info, readme)
            # Start webrowser with url (can trigger twice)
            webbrowser.open_new(f'http://{URL}:{PORT}/')
        else:
            # Add repo_paths
            add_repo_path(add_all, group_name, set_name, repo_paths, info, readme)
    try:
        app.run(host=URL, port=port, debug=True)
    except OSError:
        print("Address already in use!")

if __name__ == "__main__":
    run()
