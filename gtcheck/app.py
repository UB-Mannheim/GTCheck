#!/usr/bin/env python
import imghdr
import logging
import os
import re
import sys
import time
import webbrowser
from functools import lru_cache
from logging import Formatter, FileHandler
from pathlib import Path

from flask import Flask, render_template, request, Markup, session, flash
from git import Repo

app = Flask(__name__)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def modifications(difftext):
    mods = []
    last_pos = 1
    for mod in re.finditer(r'(\[-(.*?)-\]|{\+(.*?)\+})', difftext):
        sub = mod[2] if mod[2] != None else ""
        add = mod[3] if mod[3] != None else ""
        if add != "" and len(mods) > 0 and last_pos == mod.regs[0][0]:
            if mods[len(mods) - 1][1] == "":
                mods[len(mods) - 1][1] = add
                continue
        last_pos = mod.regs[0][1]
        mods.append([sub, add])
    return mods


def color_diffs(difftext):
    return difftext.replace("{+", '<span style="color:green">') \
        .replace("+}", "</span>") \
        .replace("[-", '<span style="color:red">') \
        .replace("-]", "</span>")


def surrounding_images(img, folder):
    # Default  ^(.*?)(\d+)(\D*)$
    imgmatch = re.match(rf"{session['regexnum']}", img.name)
    imgint = int(imgmatch[2])
    imgprefix = img.name[:imgmatch.regs[1][1]]
    imgpostfix = img.name[imgmatch.regs[3][0]:]
    prev_img = img.parent.joinpath(imgprefix + f"{imgint - 1:0{imgmatch.regs[2][1]-imgmatch.regs[2][0]}d}" + imgpostfix)
    post_img = img.parent.joinpath(imgprefix + f"{imgint + 1:0{imgmatch.regs[2][1]-imgmatch.regs[2][0]}d}" + imgpostfix)
    if prev_img.exists():
        prev_img = Path("./symlink/").joinpath(prev_img.relative_to(folder.parent))
    else:
        prev_img = ""
    if post_img.exists():
        post_img = Path("./symlink/").joinpath(post_img.relative_to(folder.parent))
    else:
        post_img = ""
    return prev_img, post_img


#@lru_cache(maxsize=None)
def get_repo(path):
    return Repo(path, search_parent_directories=True)

def get_gitdifftext(orig, diff, repo):
    from subprocess import run, PIPE
    p = run(['git', 'hash-object', '-', '--stdin'], stdout=PIPE,
            input=orig, encoding='utf-8')
    p2 = run(['git', 'hash-object', '-', '--stdin'], stdout=PIPE,
             input=diff, encoding='utf-8')
    return repo.git.diff(p.stdout.strip(), p2.stdout.strip(), "-p", "--word-diff").split("@@")[
        -1].strip()

def get_difftext(origtext, item, folder, repo):
    if "<<<<<<< HEAD\n" in origtext:
        with open(folder.joinpath(item.a_path), "r") as fin:
            mergetext = fin.read().split("<<<<<<< HEAD\n")[-1].split("\n>>>>>>>")[0].split("\n=======\n")
        difftext = get_gitdifftext(mergetext[0],mergetext[1], repo)
    else:
        try:
            difftext = "".join(item.diff.decode('utf-8').split("\n")[1:])
        except UnicodeDecodeError as ex:
            print("Warning the diff text could not be decoded! ",ex)
            try:
                difftext = get_gitdifftext(origtext, item.b_blob.data_stream.read().decode(), repo)
            except Exception as ex2:
                print("Both files could not be compared!")
                difftext = ""
    return difftext


@app.route("/gtcheck", methods=["GET", "POST"])
def gtcheck():
    repo = get_repo(session["folder"])
    folder = Path(session["folder"])
    name = repo.config_reader().get_value("user", "name")
    name = "GTChecker" if name == "" else name
    email = repo.config_reader().get_value("user", "email")
    # Diff Head
    diffhead = repo.git.diff('--cached', '--shortstat').strip().split(" ")[0]
    #difflist =  [item for item in repo.index.diff(None, create_patch=True, word_diff_regex=".") if
    #            ".gt.txt" in "".join(Path(item.a_path).suffixes)]
    if not session['difflist'] or len(session['difflist']) <= session['skip']:
        session['difflist'] = [item.a_path for item in repo.index.diff(None) if ".gt.txt" in item.a_path]
        if len(session['difflist']) <= session['skip']:
            session['difflist'] = [None]*session['skip']
        else:
            session['difflen'] = len(session['difflist'])
            session['difflist'] = session['difflist'][:session['skip']+100]
    difflist = session['difflist'][session['skip']:]
    for fileidx, filename in enumerate(difflist):
        item = repo.index.diff(None, paths=[filename], create_patch=True, word_diff_regex=".")
        if item:
            item = item[0]
        else:
            item = repo.index.diff(None, paths=[filename])[0]
        if not item.a_blob and not item.b_blob:
            session['difflist'].pop(session['skip']+fileidx)
            continue
        session['modtype'] = "mod"
        mergetext = []
        origtext = item.a_blob.data_stream.read().decode('utf-8').lstrip(" ")
        difftext = get_difftext(origtext, item, folder, repo)
        diffcolored = color_diffs(difftext)
        if origtext == "" and not item.deleted_file or item.new_file:
            session['modtype'] = "new"
            diffcolored = "<span style='color:green'>This untracked file gets added when committed and deleted when stashed!</span>"
        if item.deleted_file or not item.b_path:
            session['modtype'] = "del"
            modtext = ""
            diffcolored = "<span style='color:red'>This file gets deleted when committed and restored when stashed!</span>"
        elif mergetext:
            session['modtype'] = "merge"
            modtext = mergetext[1]
        else:
            modtext = folder.absolute().joinpath(item.b_path).open().read().lstrip(" ")
        if origtext.strip() == modtext.strip() and session['skipcc']:
            if session["addcc"]:
                repo.git.add(str(filename), u=True)
            else:
                session["skip"]+=1
            continue
        fname = folder.joinpath(item.a_path)
        mods = modifications(difftext)
        if diffhead:
            commitmsg = f"[GT Checked] Staged Files: {diffhead}"
        else:
            commitmsg = f"[GT Checked]  {item.a_path}: {', '.join([orig + ' -> ' + mod for orig, mod in mods])}"
        session['modtext'] = modtext
        session['fname'] = str(fname)
        session['fpath'] = str(item.a_path)
        session['fileidx'] = fileidx
        imgfolder = Path(__file__).resolve().parent.joinpath(f"static/symlink/{folder.name}")
        # Create symlink to imagefolder
        if not imgfolder.exists():
            imgfolder.symlink_to(folder)
        inames = [iname for iname in fname.parent.glob(f"{fname.name.replace('gt.txt', '')}*") if imghdr.what(iname)]
        img = inames[0] if inames else None
        if not img:
            return render_template("gtcheck.html", repo=session["folder"], branch=repo.active_branch, name=name,
                                   email=email, commitmsg=commitmsg,
                                   difftext=Markup(diffcolored), origtext=origtext, modtext=modtext,
                                   files_left=str(session['difflen']-session['skip']),
                                   iname="No image", fname=str(fname.name), skipped=session['skip'])
        else:
            img_out = Path("./symlink/").joinpath(img.relative_to(folder.parent))
            prev_img, post_img = surrounding_images(img, folder)
            return render_template("gtcheck.html", repo=session["folder"], branch=repo.active_branch, name=name,
                                   email=email, commitmsg=commitmsg, image=img_out, previmage=prev_img,
                                   postimage=post_img,
                                   difftext=Markup(diffcolored), origtext=origtext, modtext=modtext,
                                   files_left=str(session['difflen']-session['skip']),
                                   iname=str(img.name), fname=str(fname.name), skipped=session['skip'])
    else:
        if diffhead:
            commitmsg = f"[GT Checked] Staged Files: {diffhead}"
            modtext = f"Please commit the staged files! You skipped {session['skip']} files."
            return render_template("gtcheck.html", name=name, email=email, commitmsg=commitmsg, modtext=modtext,
                                   files_left="0")
        if not session['difflist']:
            return render_template("nofile.html")
        session["skip"] = 0
        return gtcheck()

def pop_idx(lname, popidx):
    if len(session[lname]) > popidx:
        session[lname].pop(popidx)
    return

@app.route("/gtcheckedit", methods=["GET", "POST"])
def gtcheckedit():
    repo = get_repo(session["folder"])
    fname = Path(session["folder"]).joinpath(session['fpath'])
    data = request.form  # .to_dict(flat=False)
    modtext = data['modtext'].replace("\r\n","\n")
    if data['selection'] == 'commit':
        if session['difflen']-session['skip'] != 0:
            if session['modtext'].replace("\r\n","\n") != modtext or session['modtype'] == "merge":
                with open(fname, "w") as fout:
                    fout.write(modtext)
            repo.git.add(str(fname), u=True)
        repo.git.commit('-m', data["commitmsg"])
        session['difflist'] = []
    elif data['selection'] == 'stash':
        if session['modtype'] in ["new"]:
            repo.git.rm('-f', str(fname))
        else:
            repo.git.checkout('--', str(fname))
            # Used stash push but it seems to have negative side effects
            #repo.git.stash('push', str(fname))
    elif data['selection'] == 'add':
        if session['modtext'].replace("\r\n","\n") != modtext or session['modtype'] == "merge":
            with open(fname, "w") as fout:
                fout.write(modtext)
        repo.git.add(str(fname), u=True)
    else:
        session['skip'] += 1
        return gtcheck()
    pop_idx('difflist', session['skip'] + session['fileidx'])
    return gtcheck()


@app.route("/gtcheckinit", methods=["POST"])
def gtcheckinit():
    data = request.form  # .to_dict(flat=False)
    folder = data['repo']
    repo = get_repo(folder)
    session.clear()
    session["folder"] = folder
    session["skip"] = 0
    session['difflist'] = []
    session['addcc'] = True if 'addCC' in data.keys() else False
    session['skipcc'] = True if 'skipCC' in data.keys() else False
    session['regexnum'] = data['regexnum']
    # untracked files to potential add
    [repo.git.add("-N", item) for item in repo.untracked_files if ".gt.txt" in item]
    if data["branches"] != repo.active_branch:
        assert repo.untracked_files, "Untracked files detected, please resolve for checkout branches"
        # repo.git.checkout(data["branches"])
    if data.get("checkout", "off") == "on" and data["new_branch"] != "":
        assert repo.untracked_files, "Untracked files detected, please resolve for checkout branches"
        repo.git.checkout(data["checkout"], b=data["new_branch"])
        # Check requirements
    assert not repo.bare, "Git repo is bare"  # check if repo is bare
    assert repo.is_dirty(), "No modified gt-files in the repository"  # check the dirty state
    return gtcheck()


def clean_symlinks():
    symlinkfolder = Path(__file__).resolve().parent.joinpath(f"static/symlink/")
    for folder in symlinkfolder.iterdir():
        if folder.is_dir():
            folder.unlink()
    return


@app.route("/")
def index():
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
    else:
        folder = Path(".")
    repo = get_repo(folder)
    folder = Path(repo.git_dir).parent
    name = repo.config_reader().get_value("user", "name")
    clean_symlinks()
    if name == "":
        name = "GTChecker"
    email = repo.config_reader().get_value("user", "email")
    diffhead = repo.git.diff('--cached', '--shortstat').strip().split(" ")[0]
    if diffhead != "":
        flash(f"You have {diffhead} staged file[s] in the {repo.active_branch} branch! These files will be added to the next commit.")
    return render_template("setup.html", name=name, email=email, repo=str(folder), active_branch=repo.active_branch,
                           branches=repo.branches)


@app.errorhandler(500)
def internal_error(error):
    print(str(error))


@app.errorhandler(404)
def not_found_error(error):
    print(str(error))


if not app.debug:
    file_handler = FileHandler('error.log')
    file_handler.setFormatter(
        Formatter('%(asctime)s %(levelname)s: \
            %(message)s [in %(pathname)s:%(lineno)d]')
    )
    #hey = logging
    #app.logger.setLevel(logging.INFO)
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)


def run():
    port = int(os.environ.get('PORT', 5000))
    app.config['SECRET_KEY'] = str(int(time.time()))
    webbrowser.open_new('http://127.0.0.1:5000/')
    app.run(host='127.0.0.1', port=port, debug=True)


if __name__ == "__main__":
    run()
