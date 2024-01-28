#!/usr/bin/env python3
import datetime
import imghdr
import json
import logging
import math
import os
import re
import shutil
import webbrowser
from collections import defaultdict
from configparser import NoSectionError
from hashlib import sha1, sha256
from logging import Formatter
from logging.handlers import RotatingFileHandler
from pathlib import Path
from subprocess import check_output

import markdown
from flask import (Flask, Markup, flash, redirect, render_template, request,
                   session, url_for)
from git import GitCommandError, InvalidGitRepositoryError, Repo

from .config import (ADMINPASSWORD, DATA_DIR, LOG_DIR, PORT, SECRET_KEY,
                     SUBREPO_DIR, SYMLINK_DIR, URL, USERPASSWORD)

app = Flask(__name__, instance_path=str(Path(__file__).parent.resolve().joinpath("instance")))


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
    :param regex: regexpattern to extract the pagenumber
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
            # The UnicodeDecodeError mostly appears if the original character is a combination of unicode symbols
            # e.g. ä -> e+diacritic_mark and the modified character only differs in one and not all parts e.g. ö.
            app.logger.warning(f"File:{item.a_path} Warning the diff text could not be decoded! Error:{ex}")
            try:
                difftext = get_gitdifftext(origtext, item.b_blob.data_stream.read().decode(), repo)
            except Exception as ex2:
                app.logger.warning(f"File:{item.a_path} Both files could not be compared! Error:{ex2}")
                difftext = ""
    return difftext


class GTDiffObject(object):

    def __init__(self, repo, repo_path, filename, add_all=False):
        self.repo = repo
        self.repo_path = repo_path
        self.filename = filename
        self.fpath = self.repo_path.joinpath(filename)
        self.item = None
        self.origtext = ''
        self.difftext = ''
        self.modtext = ''
        self.modtype = ''
        self.diffcolored = ''
        self.__diff__(add_all)

    def __diff__(self, add_all):
        self.item = self.repo.index.diff(None, paths=[self.filename], create_patch=True, word_diff_regex='.')
        # TODO: Check if this is necessary
        if not self.item:
            self.item = self.repo.index.diff('HEAD', paths=[self.filename], create_patch=True, word_diff_regex='.')
        if not self.item and add_all:
            self.path = self.filename
            self.origtext = ""
            self.difftext = open(Path(self.repo.working_dir).joinpath(self.filename), 'r').read()
            self.modtext = self.difftext
            self.modtype = "new"
            self.diffcolored = "<span style='color:green'>The file gets added " \
                          "when committed and deleted when stashed!</span>"
        else:
            if self.item:
                self.item = self.item[0]
            else:
                #self.item = None
                self.repo.git.add('-A', self.filename)
                item = self.repo.index.diff('HEAD', paths=[self.filename])
                if item != []:
                    self.item = item[0]
                else:
                    self.item = None

            if self.item and (self.item.a_blob or self.item.b_blob):
                self.get_modtext()

    def get_modtext(self):
        self.modtype = self.repo.git.status('--porcelain', self.filename).split(' ')[0]
        try:
            self.origtext = self.item.a_blob.data_stream.read().decode('utf-8').lstrip(" ")
            self.path = self.item.a_path
        except:
            self.origtext = ""
            self.path = self.item.b_path
        self.mergetext = []
        if self.modtype == "A":
            if self.origtext != "":
                self.modtext = self.origtext
                self.origtext = ""
            self.modtype = "new"
            self.diffcolored = "<span style='color:green'>This untracked file gets added " \
                          "when committed and deleted when stashed!</span>"
            return
        elif self.modtype == "D":
            if self.modtext != "":
                self.modtext = self.origtext
                self.origtext = ""
            self.modtype = "del"
            self.modtext = ""
            self.diffcolored = "<span style='color:red'>This file gets deleted " \
                          "when committed and restored when stashed!</span>"
            return
        self.difftext = get_difftext(self.origtext, self.item, self.repo_path, self.repo)
        self.diffcolored = color_diffs(self.difftext)
        if self.modtype == "M":
            self.modtype = "merge"
            self.modtext = self.mergetext[1]
        else:
            self.modtext = self.repo_path.absolute().joinpath(self.item.b_path).open().read().lstrip(" ")

    def diff_only_in_cc(self):
        return self.origtext.strip() == self.modtext.strip() and self.origtext.strip() != ""

    def validate_filter(self, filter_all, filter_from, filter_to):
        # Apply filter options
        if filter_all + filter_from + filter_to != '':
            if filter_all != '':
                if not (re.search(rf"{filter_all}", self.origtext) or
                        re.search(rf"{filter_all}", self.modtext)):
                   return True
            if filter_from + filter_to != '':
                origdiff, moddiff = get_diffs(self.difftext)
                if not (re.search(rf"{filter_from}", origdiff) and
                        re.search(rf"{filter_to}", moddiff)):
                   return True
        return False

    def mods(self):
        return modifications(self.difftext)


def from_list_to_list(repo_data, from_list='diff_list', to_list='skipped', index=0, all=False):
    if all:
        repo_data[to_list] = repo_data[from_list]+repo_data[to_list]
        repo_data[from_list] = []
    else:
        repo_data[to_list].append(repo_data[from_list].pop(index))
    return


@app.route('/gtcheck/edit/<group_name>/<repo_path_hash>/<subrepo>', methods=['GET', 'POST'])
def gtcheck(group_name, repo_path_hash, subrepo, repo=None, repo_data=None):
    """
    Gathers the information to render the gtcheck
    :return:
    """
    repo_data_path = get_repo_data_path(group_name, repo_path_hash, subrepo)
    if repo_data is None:
        repo_data = get_repo_data(repo_data_path)
    if repo is None:
        repo = get_repo(repo_data.get('path'))
    repo_path = Path(repo_data.get('path'))
    username, email = get_git_credentials(repo)
    # Diff Head
    diff_head = repo.git.diff('--cached', '--shortstat').strip().split(" ")[0]
    if not repo_data.get('diff_list') or len(repo_data.get('diff_list')) <= 0:
        repo_data['diff_list'] = alphanum_sort([item.a_path for item in repo.index.diff(None) if ".gt.txt" in item.a_path])
    diff_list = repo_data.get('diff_list')[:]
    for filename in diff_list:
        gtdiff = GTDiffObject(repo, repo_path, filename)
        if gtdiff.item is None:
            from_list_to_list(repo_data, from_list='diff_list', to_list='removed_list')
            continue
        if gtdiff.diff_only_in_cc() and repo.data('skipcc'):
            if repo_data.get('addcc', None) is not None:
                from_list_to_list(repo_data, from_list='diff_list', to_list='finished_list')
                repo.git.add(str(filename), A=True)
            else:
                from_list_to_list(repo_data, from_list='diff_list', to_list='skipped_list')
            continue
        if gtdiff.validate_filter(repo_data.get('filter_all'), repo_data.get('filter_from'), repo_data.get('filter_to')):
            from_list_to_list(repo_data, from_list='diff_list', to_list='skipped_list')
            continue
        if diff_head:
            commitmsg = f"Staged Files: {diff_head}"
        else:
            commitmsg = f"{repo_path.name}: {', '.join([orig + ' -> ' + mod for orig, mod in gtdiff.mods()])}"
        repo_data['modtext'] = gtdiff.modtext
        repo_data['modtype'] = gtdiff.modtype
        repo_data['fname'] = str(gtdiff.fpath)
        repo_data['fpath'] = str(gtdiff.path)
        custom_keys = [' '.join(repo_data.get('custom_keys')[i:i + 10]) for i in
                       range(0, len(repo_data.get('custom_keys')), 10)]
        if repo_data['mode'] == 'main':
            inames = [Path(SYMLINK_DIR).joinpath(repo_path_hash).joinpath(str(iname.relative_to(repo_data['path'])))
                      for iname in gtdiff.fpath.parent.glob(f"{gtdiff.fpath.name.replace('gt.txt', '')}*")
                      if imghdr.what(iname)]
        else:
            inames = [Path(SYMLINK_DIR).joinpath(repo_path_hash).joinpath(str(iname.relative_to(repo_data['parent_repo_path']))) for iname in
                      Path(repo_data['parent_repo_path']).glob(f"{str(gtdiff.fpath.relative_to(repo_data.get('path'))).replace('gt.txt', '')}*")
                      if imghdr.what(iname)]
        img_out = inames[0] if inames else None
        if not img_out:
            write_repo_data(repo_data_path, repo_data)
            return render_template("gtcheck.html", repo_data=repo_data, repo_path_hash=repo_path_hash, subrepo=subrepo,
                                   group_name=group_name,
                                   branch=repo.active_branch, username=username,
                                   email=email, commitmsg=commitmsg,
                                   difftext=Markup(gtdiff.diffcolored), origtext=gtdiff.origtext, modtext=gtdiff.modtext,
                                   files_left=str(len(repo_data.get('diff_list'))),
                                   iname="No image", fname=gtdiff.fpath.name, skipped=len(repo_data.get('skipped_list')),
                                   vkeylang=repo_data.get('vkeylang'), custom_keys=custom_keys,
                                   font=repo_data.get('font'))
        else:
            prev_img, post_img = surrounding_images(img_out, repo_data.get('regexnum'))
            write_repo_data(repo_data_path, repo_data)
            return render_template("gtcheck.html", repo_data=repo_data, repo_path_hash=repo_path_hash, subrepo=subrepo,
                                   group_name=group_name,
                                   branch=repo.active_branch, username=username,
                                   email=email, commitmsg=commitmsg,
                                   image=str(Path(img_out).relative_to(Path(SYMLINK_DIR).parent)),
                                   previmage=str(Path(prev_img).relative_to(Path(
                                       SYMLINK_DIR).parent)) if prev_img != "" else "",
                                   postimage=str(Path(post_img).relative_to(Path(
                                       SYMLINK_DIR).parent)) if post_img != "" else "",
                                   difftext=Markup(gtdiff.diffcolored), origtext=gtdiff.origtext,
                                   modtext=gtdiff.modtext,
                                   files_left=str(len(repo_data.get('diff_list'))),
                                   iname=img_out.name, fname=gtdiff.fpath.name,
                                   skipped=len(repo_data.get('skipped_list')),
                                   vkeylang=repo_data.get('vkeylang'), custom_keys=custom_keys,
                                   font=repo_data.get('font'))
    else:
        if diff_head:
            commitmsg = f"Staged Files: {diff_head}"
            modtext = f"Please commit the staged files! You skipped {len(repo_data.get('skipped_list'))} files."
            write_repo_data(repo_data_path, repo_data)
            return render_template("gtcheck.html", repo_data=repo_data, repo_path_hash=repo_path_hash, subrepo=subrepo,
                                   group_name=group_name,
                                   username=username, email=email, commitmsg=commitmsg, modtext=modtext, custom_keys='',
                                   files_left="0")
        if repo_data.get('diff_list', None) == []:
            write_repo_data(repo_data_path, repo_data)
            return render_template("nofile.html")
        write_repo_data(repo_data_path, repo_data)
        return gtcheck(group_name, repo_path_hash, subrepo, repo, repo_data)


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
    """ Set the git credentials name and email address."""
    try:
        if Path(repo.git_dir).joinpath('config.lock').exists():
            Path(repo.git_dir).joinpath('config.lock').unlink()
        repo.config_writer().set_value(level, 'name', username).release()
        repo.config_writer().set_value(level, 'email', email).release()
    except:
        pass


def get_git_credentials(repo, level='repository'):
    """ Return the git credentials name and email address."""
    username, email = "", ""
    try:
        username = repo.config_reader().get_value(level, 'name')
        email = repo.config_reader().get_value(level, 'email')
    except NoSectionError:
        pass
    return username, email


@app.route('/gtcheck/edit/update/<group_name>/<repo_path_hash>/<subrepo>', methods=['GET', 'POST'])
def edit(group_name, repo_path_hash, subrepo):
    """
    Process the user input from gtcheck html pages
    :return:
    """
    data = request.form  # .to_dict(flat=False)
    repo_data_path = get_repo_data_path(group_name, repo_path_hash, subrepo)
    repo_data = get_repo_data(repo_data_path)
    repo = get_repo(repo_data.get('path'))
    # Check if mod files left
    difflen = len(repo_data.get('diff_list'))
    repo_data['last_action'] = f"{datetime.date.today()}"
    repo_data['font'] = data.get('fonts', None) if data.get('fonts', None) else repo_data['font']
    if repo_data['username'] != data.get('username') or repo_data['email'] != data.get('email'):
        repo_data['username'] = data.get('username', '')
        session['username'] = data.get('username', '')
        repo_data['email'] = data.get('email', '')
        session['email'] = data.get('email', '')
        set_git_credentials(repo, data.get('username', ''), data.get('email', ''))
    repo_data['vkeylang'] = data['vkeylang']
    repo_data['custom_keys'] = data['custom_keys'].split(' ')
    if data['selection'] == 'settings':
        write_repo_data(repo_data_path, repo_data)
        return render_template("setup.html", username=data.get('username'), email=data.get('email'),
                        repo_path=data.get('repo_path', ''), group_name=group_name, subrepo=subrepo,
                        repo_path_hash=repo_path_hash,
                        active_branch=repo.active_branch.name,
                        branches=[branch.name for branch in repo.branches] if repo.branches != [] else [
                            repo.active_branch.name],
                        regexnum=repo_data.get('regexnum', "^(.*?)(\d+)(\D*)$"),
                        custom_keys=' '.join(repo_data.get('custom_keys', [''])),
                        filter_all=repo_data.get('filter_all', ''),
                        filter_from=repo_data.get('filter_from', ''),
                        filter_to=repo_data.get('filter_to', ''), )
    if data['selection'] == 'filter':
        for group in ['skipped_list', 'finished_list']:
            valid_gtfname = []
            for gtfname in repo_data[group]:
                with open(Path(repo_data['path']).joinpath(gtfname), 'r') as fin:
                    if re.search(rf"{data.get('filter')}", fin.read()):
                        valid_gtfname.append(gtfname)
            repo_data[group] = list(set(repo_data[group]).difference(set(valid_gtfname)))
            repo_data['diff_list'] = valid_gtfname + repo_data['diff_list']
        write_repo_data(repo_data_path, repo_data)
        return gtcheck(group_name, repo_path_hash, subrepo, repo, repo_data)
    if data['selection'] == 'skipped' or (difflen == 0 and len(repo_data['skipped_list']) != 0):
        if data['selection'] == 'commit':
            repo.git.commit('-m', '[GTCheck] ' + data['commitmsg'])
        from_list_to_list(repo_data, from_list='skipped_list', to_list='diff_list', all=True)
        write_repo_data(repo_data_path, repo_data)
        return gtcheck(group_name, repo_path_hash, subrepo, repo, repo_data)
    fname = Path(repo_data.get('path')).joinpath(repo_data.get('fpath'))
    modtext = data['modtext'].replace("\r\n", "\n")
    if data['selection'] == 'undo':
        if repo_data['undo_fpath'] == '':
            return gtcheck(group_name, repo_path_hash, subrepo, repo, repo_data)
        #repo.git.reset('HEAD', .repo_dataget('undo_fpath'))
        with open(repo_data.get('undo_fpath'), "w") as fout:
            fout.write(repo_data.get('undo_value'))
        undo_fname = str(repo_data.get('undo_fpath')).replace(repo_data.get('path'), '').strip('/ ')
        if undo_fname in repo_data.get('skipped_list'):
            from_list = 'skipped_list'
        elif undo_fname in repo_data.get('finished_list'):
            from_list = 'finished_list'
        else:
            from_list = 'removed_list'
        repo_data['diff_list'] = [repo_data[from_list].pop(-1)] + repo_data['diff_list']
        repo_data['undo_fpath'] = ''
        write_repo_data(repo_data_path, repo_data)
        return gtcheck(group_name, repo_path_hash, subrepo, repo, repo_data)
    repo_data['undo_fpath'] = str(fname)
    repo_data['undo_value'] = repo_data.get('modtext')
    to_list = 'finished_list'
    if data['selection'] == 'commit':
        if repo_data.get('modtext').replace("\r\n", "\n") != modtext or repo_data.get('modtype') == "merge":
            with open(fname, 'w') as fout:
                fout.write(modtext)
        repo.git.add(str(fname), A=True)
        #repo.git.add(str(fname))
        repo.git.commit('-m', '[GTCheck] '+data['commitmsg'])
    elif data['selection'] == 'stash' and difflen != 0:
        if repo_data.get('modtype') in ['new']:
            repo.git.rm('-f', str(fname))
            to_list = 'removed_list'
        else:
            repo.git.checkout('--', str(fname))
    elif data['selection'] == 'add' and difflen != 0:
        if repo_data.get('modtext').replace("\r\n", "\n") != modtext or repo_data.get('modtype') == "merge":
            with open(fname, 'w') as fout:
                fout.write(modtext)
        repo.git.add(str(fname), A=True)
    else:
        to_list = 'skipped_list'
    if difflen != 0:
        from_list_to_list(repo_data, from_list='diff_list', to_list=to_list)
    write_repo_data(repo_data_path, repo_data)
    return gtcheck(group_name, repo_path_hash, subrepo, repo, repo_data)


@app.route('/gtcheck/init/<group_name>/<repo_path_hash>/<subrepo>', methods=['POST'])
def init(group_name, repo_path_hash, subrepo):
    """
    Process user input from setup page.
    Initial set the session-variables, which are stored in a cookie.
    Triggers first render of gtcheck html page
    :return:
    """
    data = request.form  # .to_dict(flat=False)
    repo_path = data.get('repo_path', '')
    repo = get_repo(repo_path)
    repo_data_path = get_repo_data_path(group_name, repo_path_hash, subrepo=subrepo)
    set_git_credentials(repo, data.get('username', 'GTChecker'), data.get('email', ''))
    logger(str(Path(LOG_DIR).joinpath(f"{repo_path_hash}_{repo.active_branch}.log".replace(' ', '_')).resolve()))
    session['username'] = data.get('username', 'GTChecker')
    session['email'] = data.get('email', '')
    update_repo_data(repo_data_path, {'username': data.get('username', 'GTChecker'),
                                      'email': data.get('email', ''),
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
    return gtcheck(group_name, repo_path_hash, subrepo, repo)


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
def show_readme():
    """Show readme file from gt repository"""
    readme_file = request.args.get('readme_file', '')
    with open(readme_file, "r") as fin:
        md_template_string = markdown.markdown(
            fin.read(), extensions=["fenced_code"])
    return md_template_string


@app.route('/info', methods=['GET', 'POST'])
def info():
    """Show additional information"""
    return render_template("info.html")


@app.route('/seteditor/<group_name>/<repo_path_hash>', methods=['GET', 'POST'])
def seteditor(group_name, repo_path_hash):
    """
    Display edit option for ground truth sets
    :return:
    """
    repo_data_path = get_repo_data_path(group_name, repo_path_hash)
    repo_data = get_repo_data(repo_data_path)
    return render_template("seteditor.html",
                           group_name=group_name,
                           repo_path_hash=repo_path_hash,
                           setname=Path(repo_data["path"]).name)


@app.route('/edit_gtset/<group_name>/<repo_path_hash>', methods=['GET', 'POST'])
def edit_gtset(group_name, repo_path_hash):
    """
    Process the user input from seteditor
    :return:
    """
    try:
        data = request.form  # .to_dict(flat=False)
        if data.get('cancel', None) is not None:
            return redirect('/')
        repo_data_path = get_repo_data_path(group_name, repo_path_hash)
        repo_data = get_repo_data(repo_data_path)
        repo = get_repo(repo_data['path'])
        if data.get('add_all', None) is not None:
            diff_list = get_all_gt_files(repo)
        else:
            diff_list = repo_data.get('diff_list')+repo_data.get('skipped_list')
        if diff_list:
            splits = int(data.get('splits', '0')) if int(data.get('splits', '0')) >= 0 else 0
            duplications = int(data.get('duplications', '1')) if int(data.get('duplications', '0')) > 0 or splits > 0 else 0
            if splits + duplications == 0:
                update_repo_data(repo_data_path, {'diff_list': diff_list,
                                                  'skipped_list': [],
                                                  'finished_list': [],
                                                  'removed_list': [],
                                                  'diff_overall': len(diff_list),
                                                  })
                return redirect('/')
            if splits > 0:
                if data.get('splitmode', None) == 'splitmode_parts':
                    amount_per_parts = int(math.ceil(len(diff_list) / splits))
                else:
                    amount_per_parts = splits
            else:
                amount_per_parts = len(diff_list)
            dirty_repo = repo.is_dirty()
            if dirty_repo:
                repo.git.add('--all', A=True)
                repo.git.commit("-m", "[GTCheck] Commit to be reset.")
                repo.git.checkout('HEAD^1')
            # Create new git repos for duplicates and splits
            set_name = repo_data.get('name', '') if repo_data.get('name', '') != '' else \
                Path(repo_data.get('path', '')).name
            sub_repo_set = defaultdict()
            duplication_offset = 0
            while duplication_offset < 100:
                if not Path(SUBREPO_DIR).joinpath(repo_path_hash).joinpath(f'duplicate_{duplication_offset + 1:02d}_part_{1:02d}').exists():
                    break
                duplication_offset += 1
            for duplication in range(duplication_offset, duplication_offset+duplications):
                for part, amount_per_part_offset in enumerate(range(0, len(diff_list), amount_per_parts)):
                    # Init sub_repo under static/symlink folder
                    sub_repo_ext = f'duplicate_{duplication + 1:02d}_part_{part + 1:02d}'
                    sub_repo_path = Path(SUBREPO_DIR).joinpath(repo_path_hash).joinpath(sub_repo_ext)
                    sub_repo_path.mkdir(parents=True, exist_ok=True)
                    sub_repo_set[repo_path_hash + sub_repo_ext] = Repo.init(str(sub_repo_path.resolve()))
                    sub_repo_set[repo_path_hash + sub_repo_ext].git.commit('--allow-empty', '-m', '[GTCheck] Initial empty commit.')
                    # Copy README
                    if repo_data.get('readme', None) is not None and Path(repo_data.get('readme')).is_file():
                        shutil.copy(repo_data.get('readme', None), sub_repo_path)
                    # Copy state 0 files
                    for gtfile in diff_list[amount_per_part_offset:amount_per_part_offset + amount_per_parts]:
                        src = Path(repo_data['path']).joinpath(gtfile)
                        dest = sub_repo_path.joinpath(gtfile)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if src.exists():
                            shutil.copy(src, dest)
                        # Copy also exstings json files
                        src = Path(repo_data['path']).joinpath(gtfile.replace('.gt.txt', '') + '.json')
                        if src.exists():
                            shutil.copy(src, sub_repo_path.joinpath(gtfile.replace('.gt.txt', '') + '.json'))
                    # Add repo to data folder
                    if not dirty_repo:
                        info = (repo_data.get('info', '') + " This repo is a duplicate and/or splitted into parts.").strip()
                        add_subrepo_path(True, repo_data.get('fileformat'), repo_data.get('image_dir'), group_name, set_name + "_" + sub_repo_ext, sub_repo_path, repo_data.get('path'),
                                     info, "")
            if dirty_repo:
                repo.git.checkout('master')
                # Copy newest version of files
                for duplication in range(duplication_offset, duplication_offset+duplications):
                    for part, amount_per_part_offset in enumerate(range(0, len(diff_list), amount_per_parts)):
                        sub_repo_ext = f'duplicate_{duplication + 1:02d}_part_{part + 1:02d}'
                        sub_repo_path = Path(SUBREPO_DIR).joinpath(repo_path_hash).joinpath(sub_repo_ext)
                        sub_repo = Repo(str(sub_repo_path.resolve()))
                        sub_repo.git.add('--all', A=True)
                        sub_repo.git.commit('-m', '[GTCheck] Add original state of modified files.')
                        for gtfile in diff_list[amount_per_part_offset:amount_per_part_offset + amount_per_parts]:
                            src = Path(repo_data['path']).joinpath(gtfile)
                            dest = sub_repo_path.joinpath(gtfile)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if src.exists():
                                shutil.copy(src, dest)
                            elif dest.exists():
                                dest.unlink()
                        info = (repo_data.get('info', '') + " This repo is a duplicate and/or splitted into parts.").strip()
                        add_subrepo_path(True, repo_data.get('fileformat'), repo_data.get('image_dir'), group_name, set_name + "_" + sub_repo_ext, sub_repo_path, repo_data.get('path'),
                                     info, "")
                repo.git.reset('HEAD^1')
    except Exception as e:
        internal_error(e)
    return redirect('/')


@app.route('/subseteditor/<group_name>/<repo_path_hash>', methods=['GET', 'POST'])
def subseteditor(group_name, repo_path_hash):
    """
    Display edit option for ground truth sets
    :return:
    """
    repo_data_path = get_repo_data_path(group_name, repo_path_hash)
    repo_data = get_repo_data(repo_data_path)
    _, sub_repo_data = get_grp_and_sub_repo_data()
    return render_template("subseteditor.html",
                           group_name=group_name,
                           repo_path_hash=repo_path_hash,
                           sub_repo_data=sub_repo_data,
                           setname=Path(repo_data["path"]).name)


@app.route('/edit_subset/<group_name>/<repo_path_hash>', methods=['GET', 'POST'])
def edit_subset(group_name, repo_path_hash):
    """
    Process the user input from seteditor
    :return:
    """
    try:
        data = request.form  # .to_dict(flat=False)
        repo_data_path = get_repo_data_path(group_name, repo_path_hash)
        repo_data = get_repo_data(repo_data_path)
        if data.get('cancel', None) is not None:
            return redirect('/')
        if data.get('Base', None) == 'main':
            base_sets = [Path(repo_data['path'])]
        else:
            base_sets = [Path(SUBREPO_DIR).joinpath(repo_path_hash).joinpath(key.replace('Base_', '')) for key in
                         data.keys() if 'Base_' in key]
        # TODO: atm more restrictive as it has to be for testing purpose
        if data.get('splitmode', None) == 'delete_sets':
            for base_set in base_sets:
                shutil.rmtree(str(base_set.resolve()), ignore_errors=True)
                Path(DATA_DIR).joinpath(group_name).joinpath(repo_path_hash).joinpath(base_set.name + '.json').unlink()
            # print(f'Delete files. {base_sets=}')
            return redirect('/')
        compare_sets = [Path(SUBREPO_DIR).joinpath(repo_path_hash).joinpath(key.replace('Compare_', '')) for key in
                        data.keys() if 'Compare_' in key]
        if data.get('splitmode', None) in ['diff_sets', 'merge_sets']:
            if data.get('new_duplicate', None):
                new_duplicate_set = Path(SUBREPO_DIR).joinpath(repo_path_hash)
                if data.get('Base', None) != 'main':
                    new_duplicate_set = new_duplicate_set.joinpath(
                        f"duplication_{data.get('Base').split('_')[1]}-{data.get('Compare').split('_')[1]}_part_"
                        f"{'&'.join([base_set.name.split('_')[-1] for base_set in base_sets])}-"
                        f"{'&'.join([compare_set.name.split('_')[-1] for compare_set in compare_sets])}")
                else:
                    new_duplicate_set = new_duplicate_set.joinpath(f"duplication_main-{data.get('Compare').split('_')[1]}_part_"
                                                                   f"all-{'&'.join([compare_set.name.split('_')[-1] for compare_set in compare_sets])}")
                info = (repo_data.get('info', '') + " This repo is a new duplicate.").strip()
                # copy base set and compare set to new duplicate path
                new_duplicate_set_data_path = get_repo_data_path(group_name, repo_path_hash, new_duplicate_set.name)
                if new_duplicate_set.exists():
                    new_duplicate_set_repo = Repo(new_duplicate_set)
                    new_duplicate_set_data = get_repo_data(new_duplicate_set_data_path)
                else:
                    new_duplicate_set_repo = Repo.init(new_duplicate_set)
                    new_duplicate_set_repo.git.commit('--allow-empty', '-m', '[GTCheck] Initial empty commit.')
                    new_duplicate_set_data = add_subrepo_path(True, repo_data.get('fileformat'),
                                                              repo_data.get('image_dir'), group_name,
                                                              new_duplicate_set.name, new_duplicate_set,
                                                              repo_data.get('path'), info, repo_data.get('readme', ''))
                filter = new_duplicate_set_data.get('removed_list') + new_duplicate_set_data.get('finished_list')

                added_files = []

                for base_set in base_sets:
                    if data.get('Base', None) == 'main':
                        base_set_data = repo_data
                    else:
                        base_set_data = get_repo_data(get_repo_data_path(group_name, repo_path_hash, base_set.name))
                    added_files.extend(add_compare_to_base_repo(new_duplicate_set_data, base_set_data, filter=filter))
                for compare_set in compare_sets:
                    compare_set_data = get_repo_data(get_repo_data_path(group_name, repo_path_hash, compare_set.name))
                    mod_added_files = list(set(added_files).intersection(set([mod_file for mod_file in
                     compare_set_data.get('removed_list') + compare_set_data.get('finished_list') if
                     filter is None or mod_file not in filter])))
                    if mod_added_files:
                        new_duplicate_set_repo.git.add(mod_added_files)
                        new_duplicate_set_repo.git.commit(mod_added_files, '-m',
                                                          f"[GTCheck] Merged with {' ,'.join(mod_added_files)}")

                    added_files.extend(add_compare_to_base_repo(new_duplicate_set_data, compare_set_data, filter=filter))

                added_files = list(set(added_files))
                new_duplicate_set_repo.git.add('-N', added_files)
                diff_list = alphanum_sort(new_duplicate_set_data.get('diff_list', list())+added_files)
                update_repo_data_modified_diff_list(new_duplicate_set_data_path, new_duplicate_set_data, diff_list)
            else:
                for base_set in base_sets:
                    base_set_repo = Repo(base_set)
                    if data.get('Base', None) == 'main':
                        base_set_data_path = repo_data_path
                        base_set_data = repo_data
                    else:
                        base_set_data_path = get_repo_data_path(group_name, repo_path_hash, base_set.name)
                        base_set_data = get_repo_data(base_set_data_path)
                    added_files = []
                    for compare_set in compare_sets:
                        if data.get('Base', None) == 'main' or \
                                (base_set.name.rsplit('_', 1)[1] == compare_set.name.rsplit('_', 1)[1] and
                                base_set.name != compare_set.name):
                            compare_set_data = get_repo_data(get_repo_data_path(group_name, repo_path_hash, compare_set.name))
                            added_files.extend(add_compare_to_base_repo(base_set_data, compare_set_data))
                    if data.get('splitmode', None) == 'merge_sets':
                        base_set_repo.git.add(added_files)
                        base_set_repo.git.commit(added_files, '-m', f"[GTCheck] Merged with {' ,'.join([cset.name for cset in compare_sets])}")
                    diff_list = alphanum_sort([item.a_path for item in base_set_repo.index.diff(None) if '.gt.txt' in item.a_path])
                    update_repo_data_modified_diff_list(base_set_data_path, base_set_data, diff_list)
    except Exception as e:
        internal_error(e)
    return redirect('/')


def update_repo_data_modified_diff_list(repo_data_path, repo_data, diff_list):
    skipped_list = alphanum_sort(set(repo_data.get('skipped_list')).difference(set(diff_list)))
    finished_list = alphanum_sort(set(repo_data.get('finished_list')).difference(set(diff_list)))
    removed_list = alphanum_sort(set(repo_data.get('removed_list')).difference(set(diff_list)))
    update_repo_data(repo_data_path, {'diff_list': diff_list,
                                          'skipped_list': skipped_list,
                                          'finished_list': finished_list,
                                          'removed_list': removed_list,
                                          'diff_overall': len(diff_list) + len(skipped_list) + len(finished_list) + len(
                                              removed_list),
                                          'undo_fpath': '',
                                          'undo_value': ''})

def add_compare_to_base_repo(base_set_data, compare_set_data, filter=None):
    """ Replace modified files in the base repo withouth git history """
    mod_list = [mod_file for mod_file in compare_set_data.get('removed_list')+compare_set_data.get('finished_list') if filter is None or mod_file not in filter]
    added_files = []
    for fileformat in ['.gt.txt', '.json']:
        for mod_fname in mod_list:
            mod_fname = mod_fname.replace('.gt.txt', fileformat)
            mod_fpath = Path(compare_set_data.get('path')).joinpath(mod_fname)
            if mod_fpath.exists():
                base_fname = Path(base_set_data.get('path')).joinpath(mod_fname)
                shutil.copy(mod_fpath, base_fname)
                if fileformat == '.gt.txt':
                    added_files.append(mod_fname)
    return added_files


def add_compare_to_base_repo_as_remote(base_repo, compare_repo_path, commit_staged=False):
    """ Merging the compare git repo with a remote pull this keeps the git history while """
    compare_repo = Repo(compare_repo_path)
    # Add untracked files to index (--intent-to-add)
    [compare_repo.git.add('-A', item) for item in compare_repo.untracked_files if ".md" in item or '.json' in item]
    new_commit = False
    if len(compare_repo.index.diff('HEAD')) > 0 and commit_staged:
        new_commit = True
        compare_repo.git.commit('-m', f"[GTCheck] Commit {len(compare_repo.index.diff('HEAD'))} staged files.")
    if not isinstance(base_repo, Repo):
        base_repo = Repo(base_repo)
    base_repo.create_remote(compare_repo_path.name, url=compare_repo_path)
    base_repo.remote(compare_repo_path.name).fetch('--tags')
    try:
        base_repo.git.merge('--allow-unrelated-histories', '-X', 'theirs',
                            compare_repo_path.name+f"/{compare_repo.active_branch}")
    except GitCommandError as merge_conflict:
        if 'overwritten by merge:' in merge_conflict:
            conflicted_files = str(merge_conflict).split('overwritten by merge:')[-1].split('Please commit your changes')[0].strip().split('\n')
            for conflicted_file in conflicted_files:
                base_repo.git.checkout('HEAD', '--', conflicted_file.strip())
            base_repo.git.merge('--allow-unrelated-histories', '-X', 'theirs',
                                compare_repo_path.name + f"/{compare_repo.active_branch}")
        else:
            internal_error(merge_conflict)
    base_repo.delete_remote(compare_repo_path.name)
    if new_commit:
        compare_repo.git.reset('HEAD^1')


@app.route('/gtcheck/setup/<group_name>/<repo_path_hash>/<subrepo>', methods=['GET', 'POST'])
def setup(group_name, repo_path_hash, subrepo='main'):
    """
    Renders setup page
    :return:
    """
    data = request.form  # .to_dict(flat=False)
    repo = get_repo(data['repo_path'])
    if data.get('edit_gtset', None) is not None:
        return seteditor(group_name, repo_path_hash)
    if data.get('edit_subsets', None) is not None:
        return subseteditor(group_name, repo_path_hash)
    repo_data_path = get_repo_data_path(group_name, repo_path_hash, subrepo=subrepo)
    repo_data = get_repo_data(repo_data_path)
    username, email = get_git_credentials(repo)
    username = session.get('username', username)
    email = session.get('email', email)
    if data.get('reserve', None) is not None:
        reserved_by = data.get('reserved_by', '') if len(data.get('reserved_by', '')) != 0 else "GTChecker"
        update_repo_data(repo_data_path, {'reserved_since': f"{datetime.date.today()}", 'reserved_by': reserved_by})
    if data.get('reservation_cancel', None) is not None:
        update_repo_data(repo_data_path, {'reserved_since': '', 'reserved_by': ''})
        return redirect('/')
    elif data.get('squash', None) is not None:
        repo.git.reset(repo_data.get('init_head'))
        difflist = [item.a_path for item in repo.index.diff(None) if ".gt.txt" in item.a_path]
        repo.git.add(*difflist, A=True)
        repo.git.commit('-m', f'[GTCheck] Squashed-commit added {len(difflist)} files.')
        update_repo_data(repo_data_path, {'squashed': repo.head.commit.hexsha})
        return redirect('/')
    elif data.get('done', None) is not None:
        if subrepo == 'main':
            Path(DATA_DIR).joinpath(group_name).joinpath(repo_path_hash + ".json").unlink()
        else:
            Path(DATA_DIR).joinpath(group_name).joinpath(repo_path_hash).joinpath(subrepo + ".json").unlink()
        return redirect('/')
    diff_head = repo.git.diff('--cached', '--shortstat').strip().split(" ")[0]
    if diff_head != "":
        flash(
            f"You have {diff_head} staged file[s] in the {repo.active_branch.name} branch! "
            f"These files will be added to the next commit.")
    return render_template("setup.html", username=username, email=email,
                           repo_path=data.get('repo_path', ''), group_name=group_name, subrepo=subrepo,
                           repo_path_hash=repo_path_hash,
                           active_branch=repo.active_branch.name,
                           branches=[branch.name for branch in repo.branches] if repo.branches != [] else [
                               repo.active_branch.name],
                           regexnum=repo_data.get('regexnum', "^(.*?)(\d+)(\D*)$"),
                           custom_keys=' '.join(repo_data.get('custom_keys', [''])),
                           filter_all=repo_data.get('filter_all', ''),
                           filter_from=repo_data.get('filter_from', ''),
                           filter_to=repo_data.get('filter_to', ''), )


@app.route("/login", methods=['POST'])
def password_validation():
    data = request.form  # .to_dict(flat=False)
    pwd = data.get('password', "")
    if data.get('guest_login', None) is not None:
        session['password'] = ""
    elif pwd in [ADMINPASSWORD, USERPASSWORD]:
        session['password'] = pwd
    else:
        flash('The provided password is wrong!')
        return render_template("login.html", guest_login=True if USERPASSWORD == "" else False,
                               messages=["Please again!"])
    session['username'] = data.get('username', 'GTChecker')
    session['email'] = data.get('email', '')
    session.pop('_flashes', None)
    return redirect('/')


@app.route("/", methods=['GET'])
def index():
    """
    Renders setup page
    :return:
    """
    if app.config['MODE'] == 'single':
        repo = get_repo(app.config['REPO_PATH'])
        username, email = get_git_credentials(repo)
        return render_template("setup.html", username=username, email=email, repo_path=app.config['repo_path'],
                               group_name="single", repo_path_hash=hash_it(repo.working_dir), subrepo='main',
                               active_branch=repo.active_branch, branches=repo.branches,
                               regexnum="^(.*?)(\d+)(\D*)$", custom_keys="",
                               filter_all="", filter_from="", filter_to="")
    if session.get('password', None) not in [ADMINPASSWORD, USERPASSWORD]:
        return render_template("login.html",
                               guest_login=USERPASSWORD == "",
                               password_required=(ADMINPASSWORD != "" or USERPASSWORD != ""))
    grp_repo_data, sub_repo_data = get_grp_and_sub_repo_data()
    return render_template("index.html", grp_repo_data=grp_repo_data, sub_repo_data=sub_repo_data,
                           activate_edit_gtset=(ADMINPASSWORD == "" or session.get('password', None) == ADMINPASSWORD),
                           username=session.get('username', ''))


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


def get_repo(path, search_parent_directories=True):
    """
    Returns repo instance, if the subdirectory is provided it goes up to the base directory
    :param path: Repopath
    :return:
    """
    repo = None
    try:
        repo = Repo(path, search_parent_directories=search_parent_directories)
    except InvalidGitRepositoryError:
        app.logger.warning(f'Invalid gitrepository access: {path}')
        pass
    return repo


def get_grp_and_sub_repo_data():
    repo_data_info = defaultdict(defaultdict)
    sub_repo_data_info = defaultdict(lambda: defaultdict(defaultdict))
    # Secure that 'default' dict is always on top
    repo_data_info['default'] = defaultdict()
    for repo_data_path in alphanum_sort(Path(DATA_DIR).rglob("*.json")):
        with open(repo_data_path, 'r') as fin:
            repo_data = json.load(fin)
        if repo_data.get('mode', 'main') == 'main':
            repo_data_info[repo_data_path.parent.name][repo_data_path.stem] = repo_data
        else:
            sub_repo_data_info[repo_data_path.parent.name][repo_data_path.stem.rsplit('_', 2)[0]][
                repo_data_path.stem.split('_', 2)[2]] = repo_data
    if repo_data_info['default'] == {}:
        del repo_data_info['default']
    return repo_data_info, sub_repo_data_info


def get_repo_data_path(group_name, repo_path_hash, subrepo='main'):
    if subrepo == 'main':
        repo_group_dir = Path(DATA_DIR).joinpath(group_name).joinpath(repo_path_hash + ".json")
    else:
        repo_group_dir = Path(DATA_DIR).joinpath(group_name).joinpath(repo_path_hash).joinpath(subrepo + ".json")
    return repo_group_dir


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

def alphanum_sort(list):
    alphanum = lambda text: int(text) if text.isdigit() else text
    return sorted(list, key=lambda key: [alphanum(text) for text in re.split('([0-9]+)', str(key))])

def get_all_gt_files(repo):
    return [fname+'.gt.txt' for fname in alphanum_sort([str(path.relative_to(repo.working_dir)).replace('.gt.txt', '') for path in
                   Path(repo.working_dir).rglob(f"*.gt.txt")])]


def add_subrepo_path(add_all, fileformat, image_dir, group_name, set_name, repo_path, parent_repo_path, info, readme_path):
    repogroup_dir = Path(DATA_DIR).joinpath(group_name)
    repo = get_repo(repo_path)
    if repo:
        # Check requirements
        diff_list = get_all_gt_files(repo)
        # Add credentials to repository level
        username, email = get_git_credentials(repo, level='repository' if app.config.get('MODE', 'web') == 'web' else 'user')
        set_git_credentials(repo, username, email)
        repo_data_path = repogroup_dir.joinpath(repo_path.parent.name).joinpath(repo_path.name + '.json')
        if not repo_data_path.exists():
            repo_data_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                init_head = repo.head.commit.hexsha
            except ValueError:
                app.logger.warning(f'The repo contained no head commit, so it got one created: {repo_path}')
                repo.git.commit('--allow-empty', '-m', '[GTCheck] Initial empty commit.')
                init_head = repo.head.commit.hexsha
            data = {'path': str(repo_path),
                    'mode': 'sub',
                    'image_dir': image_dir,
                    'parent_repo_path': parent_repo_path,
                    'name': set_name,
                    'info': info,
                    'squashed': None,
                    'done': False,
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
                    'finished_list': [],
                    'removed_list': [],
                    'skipped_list': [],
                    'diff_overall': len(diff_list),
                    'font': 'RobotoMonoGTC',
                    'vkeylang': '',
                    'custom_keys': [],
                    'modtext': '',
                    'fname': '',
                    'fpath': '',
                    'undo_fpath': '',
                    'undo_value': ''}
            with open(repo_data_path, 'w') as fout:
                json.dump(data , fout, indent=4)
            return data


def add_repo_path(add_all, image_dir, group_name, set_name, repo_paths, info, readme, reset_to=None):
    repogroup_dir = Path(DATA_DIR).joinpath(group_name)
    if not repogroup_dir.exists():
        repogroup_dir.mkdir(parents=True, exist_ok=True)
    for repo_path in repo_paths:
        app.logger.info(repo_path + ' adding to GTCheck!')
        log = app.logger
        repo = get_repo(repo_path)
        # Purge the selection
        image_dir = Path(image_dir)
        if not image_dir.exists() or image_dir.is_file():
            image_dir = Path('.')
        if repo:
            if reset_to:
                repo.git.reset('--soft', reset_to)
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
                diff_list = alphanum_sort([item.a_path for item in repo.index.diff(None) if ".gt.txt" in item.a_path])

                if not diff_list:
                    app.logger.error(f'The repo contains no modified GT data: {repo_path}')
                    return
            else:
                diff_list = get_all_gt_files(repo)
            # Add credentials to repository level
            username, email = get_git_credentials(repo, level='repository' if app.config.get('MODE', 'web') == 'web' else 'user')
            set_git_credentials(repo, username, email)
            repo_path = repo.working_dir
            repo_path_hash = hash_it(repo_path)
            repo_data_path = repogroup_dir.joinpath(repo_path_hash + ".json")
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
                with open(repo_data_path, 'w') as fout:
                    json.dump({'path': repo_path,
                               'mode': 'main',
                               'image_dir': str(image_dir.resolve()),
                               'parent_repo_path': None,
                               'name': set_name,
                               'info': infotext,
                               'squashed': None,
                               'done': False,
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
                               'finished_list': [],
                               'skipped_list': [],
                               'removed_list': [],
                               'diff_overall': len(diff_list),
                               'font': 'RobotoMonoGTC',
                               'vkeylang': '',
                               'custom_keys': [],
                               'modtext': '',
                               'fname': '',
                               'fpath': '',
                               'undo_fpath': '',
                               'undo_value': '',
                               }, fout, indent=4)
                app.logger.info(repo_path+' succesfully added to GTCheck!')


def hash_it(string):
    return sha256(string.encode('utf-8')).hexdigest()


def run_server(purge):
    """
    Starting point to run the app as server
    :return:
    """
    # Purge the selection
    if 'all' in purge: purge = ['all']
    for purge_sel in purge:
        if purge_sel in ['symlinks', 'all']:
            purge_folder(SYMLINK_DIR, create_gitkeep=True)
            purge_folder(SUBREPO_DIR, create_gitkeep=True)
        if purge_sel in ['logs', 'all']:
            purge_folder(LOG_DIR, create_gitkeep=True)
        if purge_sel in ['repo_settings', 'all']:
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
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['MODE'] = 'web'
    try:
        app.run(host=URL, port=port, debug=True)
    except OSError:
        print("Address already in use!")


def run_single(repo_path, add_all, image_dir, set_name):
    """
    Starting point to run the app for single repo editing
    :return:
    """
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
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['MODE'] = 'single'
    app.config['REPO_PATH'] = repo_path
    add_repo_path(add_all, image_dir, "single", set_name, app.config['REPO_PATH'], "", "")
    try:
        app.run(host=URL, port=port, debug=True)
        webbrowser.open_new(f'http://{URL}:{PORT}/')
    except OSError:
        print("Address already in use!")
