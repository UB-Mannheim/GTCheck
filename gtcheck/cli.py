import click

from .app import run_server as server, run_single as single, add_repo_path
from .mets import extract_lines_from_page, update_page_by_lines


@click.group()
def gtcheck():
    pass


@gtcheck.command()
@click.argument('working-dir', nargs=1, type=click.Path(exists=True))
@click.option('-m', '--mets', default="mets.xml",
              help="METS to process, if no METS file is found a new one will be created")
@click.option('-I', '--input-file-grp', required=True, default=['./', 'GTCHECK'], nargs=2,
              help='File group(s) used as input.')
@click.option('-O', '--output-file-grp', help='File group(s) used as input.')
@click.option('-g', '--page-id', help="ID(s) of the pages to process")
@click.option('--overwrite', help="Overwrite the output file group or a page range (--page-id)", is_flag=True,
              default=False)
@click.option("--level", default="text", type=click.Choice(["text", "style", "all"]),
              help="Replace either only text or only style information or both")
def update_page(working_dir, mets, input_file_grp, output_file_grp, page_id, overwrite, level):
    if not output_file_grp:
        output_file_grp = input_file_grp[0]
    return update_page_by_lines(working_dir, mets, input_file_grp, output_file_grp, page_id, overwrite, level)


@gtcheck.command()
@click.argument('working-dir', nargs=1, type=click.Path(exists=True))
@click.option('-m', '--mets', default="mets.xml",
              help="METS to process, if no METS file is found a new one will be created")
@click.option('-I', '--input-file-grp', required=True, help='File group(s) used as input.')
@click.option('-O', '--output-file-grp', default=['GTCHECK'], help='File group(s) used as input.')
@click.option('-g', '--page-id', help="ID(s) of the pages to process")
@click.option('--overwrite', help="Overwrite the output file group or a page range (--page-id)", is_flag=True,
              default=False)
@click.option('-r', '--reset-to', default='', help='Soft reset the current commit to another commit. '
                                                   'Reset to a specific commit, e.g. "a83cde", or go a fix number of '
                                                   'commits back, e.g. reset the last three commits "HEAD^3".')
@click.option("--skip-ref", is_flag=True, help="Skips referencing to the METS")
@click.option("--outputmode", default="pair", type=click.Choice(["pair", "image", "text", "json"]),
              help="Set output ['pair' -> image, text, json, 'images' -> image, json, "
                   "'text' -> text, json, 'json' -> file]")
@click.option("--cutmode", default="bbox", type=click.Choice(["polygon", "bbox"]),
              help="Set mode polygon or bbox.")
@click.option("--feature_filter", default="",
              help="Comma-separated list of forbidden image features (e.g. `binarized,despeckled`).")
@click.option("--mimetype", default="image/png",
              type=click.Choice(["image/bmp", "application/postscript", "image/gif", "image/jpeg",
                                 "image/jp2", "image/png", "image/x-portable-pixmap", "image/tiff"]),
              help="File format to save extracted images in.")
@click.option("--transparency", default=False, is_flag=True,
              help="Add alpha channels with segment masks to the images")
def extract_page(mets, working_dir, input_file_grp, output_file_grp, page_id,
                 overwrite, reset_to, skip_ref, outputmode, cutmode, feature_filter, mimetype, transparency):
    """ Extract lines from page files \n
     This function is based on ocrd-segment-extract-line"""
    return extract_lines_from_page(mets, working_dir, input_file_grp, output_file_grp, page_id,
                                   overwrite, reset_to, skip_ref, outputmode, cutmode, feature_filter, mimetype,
                                   transparency)


@gtcheck.command()
@click.argument('repo-path', nargs=1, type=click.Path(exists=True))
@click.option('-a', '--add-all', default=False, is_flag=True, help='Add all ground truth files to the check.')
@click.option('-r', '--reset-to', default='', help='Soft reset the current commit to another commit. '
                                                   'Reset to a specific commit, e.g. "a83cde", or go a fix number of '
                                                   'commits back, e.g. reset the last three commits "HEAD^3".')
@click.option('--image-dir', default='.', type=click.Path(),
              help='Path to imagefolder (default: Images are in the same folder as the text files).')
def run_single(repo_path, add_all, image_dir, set_name):
    return single(repo_path, add_all, image_dir, set_name)


@gtcheck.command()
@click.option('-p', '--purge', multiple=True, type=click.Choice(['symlinks', 'logs', 'repo_settings', 'all']),
              help='Purge selection')
def run_server(purge):
    return server(purge)


@gtcheck.command()
@click.argument('repo-paths', nargs=-1, type=click.Path(exists=True))
@click.option('-a', '--add-all', default=False, is_flag=True, help='Add all ground truth files to the check.')
@click.option('-r', '--reset-to', help='Soft reset the current commit to another commit. '
                                       'Reset to a specific commit, e.g. "a83cde", or go a fix number of '
                                       'commits back, e.g. reset the last three commits "HEAD^3".')
@click.option('--image-dir', default='.', type=click.Path(),
              help='Path to imagefolder (default: Images are in the same folder as the text files).')
@click.option('-g', '--group-name', default="default", help='Set the gitrepo to a group')
@click.option('-s', '--set-name', default="", help='Name of the set (default: Set path)')
@click.option('-i', '--info', default="", help="Information to the GT")
@click.option('--readme', nargs=1, type=click.Path(exists=True),
              help="Add readme markdown file from gt repo manually "
                   "(default: add automatically the readme file from the main gitfolder.)")
def add_repo(add_all, reset_to, image_dir, group_name, set_name, repo_paths, info, readme):
    return add_repo_path(add_all, image_dir, group_name, set_name, repo_paths, info, readme, reset_to)


if __name__ == '__main__':
    gtcheck()
