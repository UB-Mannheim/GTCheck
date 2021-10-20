from ocrd import resolver

from pathlib import Path
import imghdr
import shutil

from ocrd_utils import EXT_TO_MIME
from ocrd.decorators import ocrd_cli_options, ocrd_cli_wrap_processor
from ocrd_segment import ReplaceLines, ExtractLines, config

from app import get_repo, Repo


def get_workspace(xmlfolder, base_name="mets.xml"):
    if not xmlfolder.exists():
        print("The working directory does not exist.")
        return
    if not xmlfolder.joinpath(base_name).exists():
        return create_mets(xmlfolder, base_name)
    return resolver.Resolver().workspace_from_url(str(xmlfolder.joinpath(base_name).resolve()))


def create_mets(xmlfolder, base_name="mets.xml"):
    """ Create a mets file for folder with plain PAGE files. """
    xmlpath = xmlfolder.joinpath(base_name)
    if xmlpath.exists(): shutil.move(xmlpath, xmlpath.with_suffix('.old.xml'))
    workspace = resolver.Resolver().workspace_from_nothing(str(xmlfolder.resolve()), mets_basename=base_name)
    for xmlfile in xmlfolder.rglob('*.xml'):
        for imgfile in [fpath for fpath in xmlpath.rglob(xmlfile.with_suffix('').name+'*') if imghdr.what(fpath)]:
            file_id = xmlfile.with_suffix('').name
            workspace.add_file('./',
                               ID=file_id+'_page',
                               mimetype=EXT_TO_MIME[xmlfile.suffix],
                               pageId=xmlfile.with_suffix('').name,
                               local_filename=str(xmlfile.relative_to(xmlpath)))
            workspace.add_file('./',
                               ID=file_id+'_img',
                               mimetype=EXT_TO_MIME[imgfile.name.replace(file_id, '')],
                               pageId=xmlfile.with_suffix('').name,
                               local_filename=imgfile.name)
    workspace.save_mets()
    return workspace


def extract_lines_from_page(mets, working_dir, input_file_grp, output_file_grp, page_id,
                            overwrite, reset_to, skip_ref, outputmode, cutmode, feature_filter, mimetype, transparency):
    """ Extract images, text and metadata from PAGE XML """
    xmlfolder = Path(working_dir)
    ws = get_workspace(xmlfolder, mets)
    # Reset to state zero
    xmlrepo = get_repo(xmlfolder, search_parent_directories=False)
    if xmlrepo:
        xmlrepo.git.commit('-m', 'GTCheck: Add original state of modified files.')
        if not reset_to: reset_to = "HEAD^1"
        xmlrepo.git.checkout(reset_to)
    # Create GTLines
    ws.save_mets()
    ocrd_cli_wrap_processor(ExtractLines,
                            input_file_grp=input_file_grp,
                            output_file_grp=output_file_grp,
                            page_id=page_id,
                            ocrd_tool=config.OCRD_TOOL,
                            mets=str(xmlfolder.joinpath(mets).resolve()),
                            working_dir=working_dir,
                            overwrite=overwrite,
                            parameter={ 'skip_ref': skip_ref,
                                        'outputmode': outputmode,
                                        'cutmode': cutmode,
                                        'feature_filter': feature_filter,
                                        'mimetype': mimetype,
                                        'transparency': transparency})
    # Init repo and init empty commit
    repo = Repo.init(xmlfolder.joinpath(output_file_grp))
    commitmsg = f'Initial commit based on {xmlrepo.git.rev_parse("HEAD")}' if xmlrepo else 'Initial commit'
    repo.git.commit('--allow-empty', '-m', commitmsg)
    # Set to original state
    if xmlrepo:
        xmlrepo.git.checkout('master')
        xmlrepo.git.reset('HEAD^1')
        # Apply GTLines again
        ocrd_cli_wrap_processor(ExtractLines,
                                input_file_grp=input_file_grp,
                                output_file_grp=output_file_grp,
                                page_id=page_id,
                                ocrd_tool=config.OCRD_TOOL,
                                mets=str(xmlfolder.joinpath(mets).resolve()),
                                working_dir=working_dir,
                                overwrite=overwrite,
                                parameter={'skip_ref': True,
                                           'output': outputmode,
                                           'cutmode': cutmode,
                                           'feature_filter': feature_filter,
                                           'mimetype': mimetype,
                                           'transparency': transparency})
    return repo


def update_page_by_lines(working_dir, mets, input_file_grp, output_file_grp, page_id, overwrite, level):
    """ Updates the text and metadata information for lines in PAGE XML files by gt-linepairs """
    mets = Path(working_dir).joinpath(mets)
    #ws = get_workspace(xmlfolder)
    # Reset to state zero
    if mets.exists():
        if input_file_grp[0] == output_file_grp:
            overwrite = True
        ocrd_cli_wrap_processor(ReplaceLines,
                                input_file_grp=','.join(input_file_grp),
                                output_file_grp=output_file_grp,
                                page_id=page_id,
                                ocrd_tool=config.OCRD_TOOL,
                                mets=str(mets.resolve()),
                                working_dir=working_dir,
                                overwrite=overwrite,
                                parameter={'level': level})
    return
