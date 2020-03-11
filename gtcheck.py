import imghdr
import re
from pathlib import Path
from git import Repo


def modifications(difftext):
    mods = []
    last_pos = 0
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
    return difftext.replace("{+",'<span style="color:green">')\
        .replace("+}","</span>")\
        .replace("[-",'<span style="color:red">')\
        .replace("-]","</span>")

def surrounding_images(folder,img):
    prev_img, post_img = "", ""
    imgnr = "".join([char for char in img.name if char.isdigit()])
    imgint = int(imgnr)
    imgprefix = img.name.split(".", 1)[0].replace(imgnr, "")
    if imgint > 0:
        prev_imgs = list(folder.joinpath(imgprefix).glob("*"+str(imgint-1)+f"*.{img.name.split('.',1)[1]}"))
        if prev_imgs:
            prev_img = prev_imgs[0]
    post_imgs = list(folder.joinpath(imgprefix).glob("*" + str(imgint + 1) + f"*.{img.name.split('.', 1)[1]}"))
    if post_imgs:
        post_img = post_imgs[0]
    return prev_img, post_img

def get_modified_files(folder):
    # Init repositorium
    repo = Repo(folder)
    # Check requirements
    assert not repo.bare, "No git repo in the folder"  # check if repo is bare
    assert repo.is_dirty(), "No modified files in the repo"  # check the dirty state
    name = repo.config_reader().get_value("user", "name")
    email = repo.config_reader().get_value("user", "email")
    #repo.config_writer().set_value("user", "name", "myusername").release()
    #repo.config_writer().set_value("user", "email", "myemail").release()
    for item in repo.index.diff(None, create_patch=True,word_diff_regex="."):
        if ".gt.txt" in "".join(Path(item.a_path).suffixes) and item.a_path == item.b_path:
            difftext = "".join(item.diff.decode('utf-8').split("\n")[1:])
            diffcolored = color_diffs(difftext)
            diffcolored += diffcolored
            orig_text = item.a_blob.data_stream.read().decode('utf-8').strip("\n ")
            mod_text = folder.absolute().joinpath(item.b_path).open().read().strip("\n ")
            fname = folder.joinpath(item.a_path)
            inames = [iname for iname in fname.parent.glob(f"{fname.name.split('.')[0]}*") if imghdr.what(iname)]
            img = inames[0] if inames else None
            prev_img, post_img = surrounding_images(fname.parent,img)
            mods = modifications(difftext)
            commitmsg = f"[GT Checked]  {item.a_path}: {', '.join([orig+' -> '+mod for orig, mod in mods])}"
            return (diffcolored,orig_text,mod_text,fname,img,commitmsg,prev_img,post_img)
    return None
"""
            # User information
            print(f"File:\t\t\t\t{Path(item.a_path)}")
            print(f"Image:\t\t\t\t{img}")
            mod_inline = ''.join(item.diff.decode('utf-8').split('\n')[1:])
            print(f"Modification inline:{mod_inline}")
            print(f"Original Text:\t\t{orig_text}")
            print(f"Modified Text:\t\t{mod_text}")
            print(f"Commit Message:\t\t{commitmsg}")

            # User input
            approve = False
            ret_text = mod_text
            ret_commitmsg = commitmsg

            if approve:
                if ret_text != mod_text:
                    fname.open().write(ret_text)
                repo.index.add(item.a_path)
                repo.index.commit(ret_commitmsg)
            else:
                repo.git.stash('push',item.a_path)
"""