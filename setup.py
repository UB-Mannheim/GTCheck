from setuptools import setup, find_packages

setup(name='gtcheck',
      version='0.6.0',
      description='Check modified gt line files in git repos',
      author='Jan Kamlah',
      url='https://github.com/jkamlah/gtcheck/',
      entry_points={
          'console_scripts': [
              'gtcheck = gtcheck.cli:gtcheck',
          ],
      },
      packages=find_packages(),
      install_requires=[
          'flask',
          'pillow',
          'click',
          'requests',
          'markdown',
          'ocrd',
          'gitpython'
      ],
      include_package_data=True,
      package_data = {'gtcheck': ['gtcheck/*', '*.py', 'favicon.ico']},
      classifiers=[
          'Environment :: Console',
          'Environment :: Web Environment',
          'Framework :: Flask',
          'Development Status :: Alpha',
          'Intended Audience :: Developers',
          'License :: OSI Approved :: Apache Software License',
          'Topic :: Software Development :: Version Control'
      ],
)
