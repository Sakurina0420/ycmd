# Copyright (C) 2011, 2012 Google Inc.
#
# This file is part of ycmd.
#
# ycmd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ycmd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ycmd.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
# Not installing aliases from python-future; it's unreliable and slow.
from builtins import *  # noqa

import ycm_core
import os
import inspect
from future.utils import PY2, native
from ycmd import extra_conf_store
from ycmd.utils import ( re, ToCppStringCompatible, OnMac, OnWindows, ToUnicode,
                         ToBytes, PathsToAllParentFolders )
from ycmd.responses import NoExtraConfDetected

# -include-pch and --sysroot= must be listed before -include and --sysroot
# respectively because the latter is a prefix of the former (and the algorithm
# checks prefixes).
INCLUDE_FLAGS = [ '-isystem', '-I', '-iquote', '-isysroot', '--sysroot',
                  '-gcc-toolchain', '-include-pch', '-include', '-iframework',
                  '-F', '-imacros', '-idirafter' ]
INCLUDE_FLAGS_WIN_STYLE = [ '/I' ]
PATH_FLAGS =  [ '--sysroot=' ] + INCLUDE_FLAGS

# We need to remove --fcolor-diagnostics because it will cause shell escape
# sequences to show up in editors, which is bad. See Valloric/YouCompleteMe#1421
STATE_FLAGS_TO_SKIP = set( [ '-c',
                             '-MP',
                             '-MD',
                             '-MMD',
                             '--fcolor-diagnostics' ] )

STATE_FLAGS_TO_SKIP_WIN_STYLE = set( [ '/c' ] )

# The -M* flags spec:
#   https://gcc.gnu.org/onlinedocs/gcc-4.9.0/gcc/Preprocessor-Options.html
FILE_FLAGS_TO_SKIP = set( [ '-MF',
                            '-MT',
                            '-MQ',
                            '-o',
                            '--serialize-diagnostics' ] )

# Use a regex to correctly detect c++/c language for both versioned and
# non-versioned compiler executable names suffixes
# (e.g., c++, g++, clang++, g++-4.9, clang++-3.7, c++-10.2 etc).
# See Valloric/ycmd#266
CPP_COMPILER_REGEX = re.compile( r'\+\+(-\d+(\.\d+){0,2})?$' )

# Use a regex to match all the possible forms of clang-cl or cl compiler
CL_COMPILER_REGEX = re.compile( r'(?:cl|clang-cl)(.exe)?$', re.IGNORECASE )

# List of file extensions to be considered "header" files and thus not present
# in the compilation database. The logic will try and find an associated
# "source" file (see SOURCE_EXTENSIONS below) and use the flags for that.
HEADER_EXTENSIONS = [ '.h', '.hxx', '.hpp', '.hh' ]

# List of file extensions which are considered "source" files for the purposes
# of heuristically locating the flags for a header file.
SOURCE_EXTENSIONS = [ '.cpp', '.cxx', '.cc', '.c', '.m', '.mm' ]

EMPTY_FLAGS = {
  'flags': [],
}


class NoCompilationDatabase( Exception ):
  pass


class Flags( object ):
  """Keeps track of the flags necessary to compile a file.
  The flags are loaded from user-created python files (hereafter referred to as
  'modules') that contain a method FlagsForFile( filename )."""

  def __init__( self ):
    # It's caches all the way down...
    self.flags_for_file = {}
    self.extra_clang_flags = _ExtraClangFlags()
    self.no_extra_conf_file_warning_posted = False

    # We cache the compilation database for any given source directory
    # Keys are directory names and values are ycm_core.CompilationDatabase
    # instances or None. Value is None when it is known there is no compilation
    # database to be found for the directory.
    self.compilation_database_dir_map = dict()

    # Sometimes we don't actually know what the flags to use are. Rather than
    # returning no flags, if we've previously found flags for a file in a
    # particular directory, return them. These will probably work in a high
    # percentage of cases and allow new files (which are not yet in the
    # compilation database) to receive at least some flags.
    # Keys are directory names and values are ycm_core.CompilationInfo
    # instances. Values may not be None.
    self.file_directory_heuristic_map = dict()


  def FlagsForFile( self,
                    filename,
                    add_extra_clang_flags = True,
                    client_data = None ):
    """Returns a tuple describing the compiler invocation required to parse the
    file |filename|. The tuple contains 2 entries:
      1. A list of the compiler flags to use,
      2. The name of the translation unit to parse.
    Note that the second argument might not be the same as the |filename|
    argument to this method in the event that the extra conf file overrides the
    translation unit, e.g. in the case of a "unity" build."""

    # The try-catch here is to avoid a synchronisation primitive. This method
    # may be called from multiple threads, and python gives us
    # 1-python-statement synchronisation for "free" (via the GIL)
    try:
      return self.flags_for_file[ filename ]
    except KeyError:
      pass

    module = extra_conf_store.ModuleForSourceFile( filename )
    try:
      results = self._GetFlagsFromExtraConfOrDatabase( module,
                                                       filename,
                                                       client_data )
    except NoCompilationDatabase:
      if not self.no_extra_conf_file_warning_posted:
        self.no_extra_conf_file_warning_posted = True
        raise NoExtraConfDetected
      return [], filename

    if not results or not results.get( 'flags_ready', True ):
      return [], filename

    return self._ParseFlagsFromExtraConfOrDatabase( filename,
                                                    results,
                                                    add_extra_clang_flags )


  def _ParseFlagsFromExtraConfOrDatabase( self,
                                          filename,
                                          results,
                                          add_extra_clang_flags ):
    if 'override_filename' in results:
      filename = results[ 'override_filename' ] or filename

    flags = _ExtractFlagsList( results )
    if not flags:
      return [], filename

    if add_extra_clang_flags:
      flags += self.extra_clang_flags
      flags = _AddMacIncludePaths( flags )

    sanitized_flags = PrepareFlagsForClang( flags,
                                            filename,
                                            add_extra_clang_flags,
                                            _ShouldAllowWinStyleFlags( flags ) )

    if results.get( 'do_cache', True ):
      self.flags_for_file[ filename ] = sanitized_flags, filename

    return sanitized_flags, filename


  def _GetFlagsFromExtraConfOrDatabase( self, module, filename, client_data ):
    if not module:
      return self._GetFlagsFromCompilationDatabase( filename )

    return _CallExtraConfFlagsForFile( module, filename, client_data )


  def Clear( self ):
    self.flags_for_file.clear()
    self.compilation_database_dir_map.clear()
    self.file_directory_heuristic_map.clear()


  def _GetFlagsFromCompilationDatabase( self, file_name ):
    file_dir = os.path.dirname( file_name )
    file_root, file_extension = os.path.splitext( file_name )

    database = self.FindCompilationDatabase( file_dir )
    compilation_info = _GetCompilationInfoForFile( database,
                                                   file_name,
                                                   file_extension )

    if not compilation_info:
      # Note: Try-catch here synchronises access to the cache (as this can be
      # called from multiple threads).
      try:
        # We previously saw a file in this directory. As a guess, just
        # return the flags for that file. Hopefully this will at least give some
        # meaningful compilation.
        compilation_info = self.file_directory_heuristic_map[ file_dir ]
      except KeyError:
        # No cache for this directory and there are no flags for this file in
        # the database.
        return EMPTY_FLAGS

    # If this is the first file we've seen in path file_dir, cache the
    # compilation_info for it in case we see a file in the same dir with no
    # flags available.
    # The following updates file_directory_heuristic_map if and only if file_dir
    # isn't already there. This works around a race condition where 2 threads
    # could be executing this method in parallel.
    self.file_directory_heuristic_map.setdefault( file_dir, compilation_info )

    return {
      'flags': _MakeRelativePathsInFlagsAbsolute(
        compilation_info.compiler_flags_,
        compilation_info.compiler_working_dir_ ),
    }


  # Return a compilation database object for the supplied path. Raises
  # NoCompilationDatabase if no compilation database can be found.
  def FindCompilationDatabase( self, file_dir ):
    # We search up the directory hierarchy, to first see if we have a
    # compilation database already for that path, or if a compile_commands.json
    # file exists in that directory.
    for folder in PathsToAllParentFolders( file_dir ):
      # Try/catch to syncronise access to cache
      try:
        database = self.compilation_database_dir_map[ folder ]
        if database:
          return database

        raise NoCompilationDatabase
      except KeyError:
        pass

      compile_commands = os.path.join( folder, 'compile_commands.json' )
      if os.path.exists( compile_commands ):
        database = ycm_core.CompilationDatabase( folder )

        if database.DatabaseSuccessfullyLoaded():
          self.compilation_database_dir_map[ folder ] = database
          return database

    # Nothing was found. No compilation flags are available.
    # Note: we cache the fact that none was found for this folder to speed up
    # subsequent searches.
    self.compilation_database_dir_map[ file_dir ] = None
    raise NoCompilationDatabase


def _ExtractFlagsList( flags_for_file_output ):
  return [ ToUnicode( x ) for x in flags_for_file_output[ 'flags' ] ]


def _ShouldAllowWinStyleFlags( flags ):
  if OnWindows():
    # Iterate in reverse because we only care
    # about the last occurrence of --driver-mode flag.
    for flag in reversed( flags ):
      if flag.startswith( '--driver-mode' ):
        return flag == '--driver-mode=cl'
    # If there was no --driver-mode flag,
    # check if we are using a compiler like clang-cl.
    return bool( CL_COMPILER_REGEX.search( flags[ 0 ] ) )

  return False


def _CallExtraConfFlagsForFile( module, filename, client_data ):
  # We want to ensure we pass a native py2 `str` on py2 and a native py3 `str`
  # (unicode) object on py3. That's the API we provide.
  # In a vacuum, always passing a unicode object (`unicode` on py2 and `str` on
  # py3) would be better, but we can't do that because that would break all the
  # ycm_extra_conf files already out there that expect a py2 `str` object on
  # py2, and WE DO NOT BREAK BACKWARDS COMPATIBILITY.
  # Hindsight is 20/20.
  if PY2:
    filename = native( ToBytes( filename ) )
  else:
    filename = native( ToUnicode( filename ) )

  # For the sake of backwards compatibility, we need to first check whether the
  # FlagsForFile function in the extra conf module even allows keyword args.
  if inspect.getargspec( module.FlagsForFile ).keywords:
    results = module.FlagsForFile( filename, client_data = client_data )
  else:
    results = module.FlagsForFile( filename )

  results[ 'flags' ] = _MakeRelativePathsInFlagsAbsolute(
      results[ 'flags' ],
      results.get( 'include_paths_relative_to_dir' ) )

  return results


def _SysRootSpecifedIn( flags ):
  for flag in flags:
    if flag == '-isysroot' or flag.startswith( '--sysroot' ):
      return True

  return False


def PrepareFlagsForClang( flags,
                          filename,
                          add_extra_clang_flags = True,
                          enable_windows_style_flags = False ):
  flags = _AddLanguageFlagWhenAppropriate( flags, enable_windows_style_flags )
  flags = _RemoveXclangFlags( flags )
  flags = _RemoveUnusedFlags( flags, filename, enable_windows_style_flags )
  if add_extra_clang_flags:
    flags = _EnableTypoCorrection( flags )

  vector = ycm_core.StringVector()
  for flag in flags:
    vector.append( ToCppStringCompatible( flag ) )
  return vector


def _RemoveXclangFlags( flags ):
  """Drops -Xclang flags.  These are typically used to pass in options to
  clang cc1 which are not used in the front-end, so they are not needed for
  code completion."""

  sanitized_flags = []
  saw_xclang = False
  for i, flag in enumerate( flags ):
    if flag == '-Xclang':
      saw_xclang = True
      continue
    elif saw_xclang:
      saw_xclang = False
      continue

    sanitized_flags.append( flag )

  return sanitized_flags


def _RemoveFlagsPrecedingCompiler( flags, enable_windows_style_flags ):
  """Assuming that the flag just before the first flag (looks like a flag,
  not like a file path) is the compiler path, removes all flags preceding it."""

  for index, flag in enumerate( flags ):
    if ( flag.startswith( '-' ) or
         ( enable_windows_style_flags and
           flag.startswith( '/' ) and
           not os.path.exists( flag ) ) ):
      return ( flags[ index - 1: ] if index > 1 else
               flags )
  return flags[ :-1 ]


def _AddLanguageFlagWhenAppropriate( flags, enable_windows_style_flags ):
  """When flags come from the compile_commands.json file, the flag preceding the
  first flag starting with a dash is usually the path to the compiler that
  should be invoked. Since LibClang does not deduce the language from the
  compiler name, we explicitely set the language to C++ if the compiler is a C++
  one (g++, clang++, etc.). Otherwise, we let LibClang guess the language from
  the file extension. This handles the case where the .h extension is used for
  C++ headers."""

  flags = _RemoveFlagsPrecedingCompiler( flags, enable_windows_style_flags )

  # First flag is now the compiler path, a flag starting with a dash or
  # a flag starting with a forward slash if enable_windows_style_flags is True.
  first_flag = flags[ 0 ]

  # NOTE: This is intentionally NOT checking for enable_windows_style_flags.
  #
  # Because of _RemoveFlagsPrecedingCompiler called above, irrelevant of
  # enable_windows_style_flags. the first flag is either the compiler
  # (path or executable), a Windows style flag or starts with a dash.
  #
  # If it doesn't start with a dash, it is either an absolute path,
  # a Windows style flag or a C++ compiler executable from $PATH.
  #   If it starts with a forward slash the flag can either be an absolute
  #   flag or a Windows style flag.
  #     If it matches the regex, it is safe to assume the flag is a compiler
  #     path.
  #     If it does not match the regex, it could still be a Windows style
  #     path or an absolute path. - This is determined in _RemoveUnusedFlags()
  #     and cleaned properly.
  #   If the flag starts with anything else (i.e. not a '-' or a '/'), the flag
  #   is a stray file path and shall be gotten rid of in _RemoveUnusedFlags().
  if ( not first_flag.startswith( '-' ) and
       CPP_COMPILER_REGEX.search( first_flag ) ):
    return [ first_flag, '-x', 'c++' ] + flags[ 1: ]
  return flags


def _RemoveUnusedFlags( flags, filename, enable_windows_style_flags ):
  """Given an iterable object that produces strings (flags for Clang), removes
  the '-c' and '-o' options that Clang does not like to see when it's producing
  completions for a file. Same for '-MD' etc.

  We also try to remove any stray filenames in the flags that aren't include
  dirs."""

  new_flags = []

  # When flags come from the compile_commands.json file, the first flag is
  # usually the path to the compiler that should be invoked. Directly move it to
  # the new_flags list so it doesn't get stripped of in the loop below.
  if not flags[ 0 ].startswith( '-' ):
    new_flags = flags[ :1 ]
    flags = flags[ 1: ]

  skip_next = False
  current_flag = flags[ 0 ]

  filename = os.path.realpath( filename )
  for flag in flags:
    previous_flag = current_flag
    current_flag = flag

    if skip_next:
      skip_next = False
      continue

    if ( flag in STATE_FLAGS_TO_SKIP or
         ( enable_windows_style_flags and
           flag in STATE_FLAGS_TO_SKIP_WIN_STYLE ) ):
      continue

    if flag in FILE_FLAGS_TO_SKIP:
      skip_next = True
      continue

    if os.path.realpath( flag ) == filename:
      continue

    # We want to make sure that we don't have any stray filenames in our flags;
    # filenames that are part of include flags are ok, but others are not. This
    # solves the case where we ask the compilation database for flags for
    # "foo.cpp" when we are compiling "foo.h" because the comp db doesn't have
    # flags for headers. The returned flags include "foo.cpp" and we need to
    # remove that.
    if _SkipStrayFilenameFlag( current_flag,
                               previous_flag,
                               enable_windows_style_flags ):
      continue

    new_flags.append( flag )

  return new_flags


def _SkipStrayFilenameFlag( current_flag,
                            previous_flag,
                            enable_windows_style_flags ):
  current_flag_starts_with_slash = current_flag.startswith( '/' )
  previous_flag_starts_with_slash = previous_flag.startswith( '/' )

  current_flag_starts_with_dash = current_flag.startswith( '-' )
  previous_flag_starts_with_dash = previous_flag.startswith( '-' )

  previous_flag_is_include = ( previous_flag in INCLUDE_FLAGS or
                               ( enable_windows_style_flags and
                                 previous_flag in INCLUDE_FLAGS_WIN_STYLE ) )

  current_flag_may_be_path = ( '/' in current_flag or
                               ( enable_windows_style_flags and
                                 '\\' in current_flag ) )

  return ( not ( current_flag_starts_with_dash or
                 ( enable_windows_style_flags and
                   current_flag_starts_with_slash ) ) and
           ( not ( previous_flag_starts_with_dash or
                   ( enable_windows_style_flags and
                     previous_flag_starts_with_slash ) ) or
             ( not previous_flag_is_include and current_flag_may_be_path ) ) )


# Return the path to the macOS toolchain root directory to use for system
# includes. If no toolchain is found, returns None.
def _SelectMacToolchain():
  # There are 2 ways to get a development enviornment (as standard) on OS X:
  #  - install XCode.app, or
  #  - install the command-line tools (xcode-select --install)
  #
  # Most users have xcode installed, but in order to be as compatible as
  # possible we consider both possible installation locations
  MAC_CLANG_TOOLCHAIN_DIRS = [
    '/Applications/Xcode.app/Contents/Developer/Toolchains/'
      'XcodeDefault.xctoolchain',
    '/Library/Developer/CommandLineTools'
  ]

  for toolchain in MAC_CLANG_TOOLCHAIN_DIRS:
    if _MacClangIncludeDirExists( toolchain ):
      return toolchain

  return None


# Ultimately, this method exists only for testability
def _GetMacClangVersionList( candidates_dir ):
  try:
    return os.listdir( candidates_dir )
  except OSError:
    # Path might not exist, so just ignore
    return []


# Ultimately, this method exists only for testability
def _MacClangIncludeDirExists( candidate_include ):
  return os.path.exists( candidate_include )


# Add in any clang headers found in the supplied toolchain. These are
# required for the same reasons as described below, but unfortuantely, these
# are in versioned directories and there is no easy way to find the "correct"
# version. We simply pick the highest version in the first toolchain that we
# find, as this is the most likely to be correct.
def _LatestMacClangIncludes( toolchain ):
  # we use the first toolchain which actually contains any versions, rather
  # than trying all of the toolchains and picking the highest. We
  # favour Xcode over CommandLineTools as using Xcode is more common.
  # It might be possible to extrace this information from xcode-select, though
  # xcode-select -p does not point at the toolchain directly
  candidates_dir = os.path.join( toolchain, 'usr', 'lib', 'clang' )
  versions = _GetMacClangVersionList( candidates_dir )

  for version in reversed( sorted( versions ) ):
    candidate_include = os.path.join( candidates_dir, version, 'include' )
    if _MacClangIncludeDirExists( candidate_include ):
      return [ candidate_include ]

  return []


MAC_INCLUDE_PATHS = []

if OnMac():
  # These are the standard header search paths that clang will use on Mac BUT
  # libclang won't, for unknown reasons. We add these paths when the user is on
  # a Mac because if we don't, libclang would fail to find <vector> etc.  This
  # should be fixed upstream in libclang, but until it does, we need to help
  # users out.
  # See the following for details:
  #  - Valloric/YouCompleteMe#303
  #  - Valloric/YouCompleteMe#2268
  toolchain = _SelectMacToolchain()
  if toolchain:
    MAC_INCLUDE_PATHS = (
      [ os.path.join( toolchain, 'usr/include/c++/v1' ),
        '/usr/local/include',
        os.path.join( toolchain, 'usr/include' ),
        '/usr/include',
        '/System/Library/Frameworks',
        '/Library/Frameworks' ] +
      _LatestMacClangIncludes( toolchain ) +
      # We include the MacOS platform SDK because some meaningful parts of the
      # standard library are located there. If users are compiling for (say)
      # iPhone.platform, etc. they should appear earlier in the include path.
      [ '/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/'
        'Developer/SDKs/MacOSX.sdk/usr/include' ]
    )


def _AddMacIncludePaths( flags ):
  if OnMac() and not _SysRootSpecifedIn( flags ):
    for path in MAC_INCLUDE_PATHS:
      flags.extend( [ '-isystem', path ] )
  return flags


def _ExtraClangFlags():
  flags = _SpecialClangIncludes()
  # On Windows, parsing of templates is delayed until instantiation time.
  # This makes GetType and GetParent commands fail to return the expected
  # result when the cursor is in a template.
  # Using the -fno-delayed-template-parsing flag disables this behavior.
  # See
  # http://clang.llvm.org/extra/PassByValueTransform.html#note-about-delayed-template-parsing # noqa
  # for an explanation of the flag and
  # https://code.google.com/p/include-what-you-use/source/detail?r=566
  # for a similar issue.
  if OnWindows():
    flags.append( '-fno-delayed-template-parsing' )
  return flags


def _EnableTypoCorrection( flags ):
  """Adds the -fspell-checking flag if the -fno-spell-checking flag is not
  present"""

  # "Typo correction" (aka spell checking) in clang allows it to produce
  # hints (in the form of fix-its) in the case of certain diagnostics. A common
  # example is "no type named 'strng' in namespace 'std'; Did you mean
  # 'string'? (FixIt)". This is enabled by default in the clang driver (i.e. the
  # 'clang' binary), but is not when using libclang (as we do). It's a useful
  # enough feature that we just always turn it on unless the user explicitly
  # turned it off in their flags (with -fno-spell-checking).
  if '-fno-spell-checking' in flags:
    return flags

  flags.append( '-fspell-checking' )
  return flags


def _SpecialClangIncludes():
  libclang_dir = os.path.dirname( ycm_core.__file__ )
  path_to_includes = os.path.join( libclang_dir, 'clang_includes' )
  return [ '-resource-dir=' + path_to_includes ]


def _MakeRelativePathsInFlagsAbsolute( flags, working_directory ):
  if not working_directory:
    return list( flags )
  new_flags = []
  make_next_absolute = False
  path_flags = ( PATH_FLAGS + INCLUDE_FLAGS_WIN_STYLE
                 if _ShouldAllowWinStyleFlags( flags )
                 else PATH_FLAGS )
  for flag in flags:
    new_flag = flag

    if make_next_absolute:
      make_next_absolute = False
      if not os.path.isabs( new_flag ):
        new_flag = os.path.join( working_directory, flag )
      new_flag = os.path.normpath( new_flag )
    else:
      for path_flag in path_flags:
        # Single dash argument alone, e.g. -isysroot <path>
        if flag == path_flag:
          make_next_absolute = True
          break

        # Single dash argument with inbuilt path, e.g. -isysroot<path>
        # or double-dash argument, e.g. --isysroot=<path>
        if flag.startswith( path_flag ):
          path = flag[ len( path_flag ): ]
          if not os.path.isabs( path ):
            path = os.path.join( working_directory, path )
          path = os.path.normpath( path )

          new_flag = '{0}{1}'.format( path_flag, path )
          break

    if new_flag:
      new_flags.append( new_flag )
  return new_flags


# Find the compilation info structure from the supplied database for the
# supplied file. If the source file is a header, try and find an appropriate
# source file and return the compilation_info for that.
def _GetCompilationInfoForFile( database, file_name, file_extension ):
  # Ask the database for the flags.
  compilation_info = database.GetCompilationInfoForFile( file_name )
  if compilation_info.compiler_flags_:
    return compilation_info

  # The compilation_commands.json file generated by CMake does not have entries
  # for header files. So we do our best by asking the db for flags for a
  # corresponding source file, if any. If one exists, the flags for that file
  # should be good enough.
  if file_extension in HEADER_EXTENSIONS:
    for extension in SOURCE_EXTENSIONS:
      replacement_file = os.path.splitext( file_name )[ 0 ] + extension
      compilation_info = database.GetCompilationInfoForFile(
        replacement_file )
      if compilation_info and compilation_info.compiler_flags_:
        return compilation_info

  # No corresponding source file was found, so we can't generate any flags for
  # this source file.
  return None


def UserIncludePaths( user_flags, filename ):
  """
  Returns a tuple ( quoted_include_paths, include_paths )

  quoted_include_paths is a list of include paths that are only suitable for
  quoted include statement.
  include_paths is a list of include paths that can be used for angle bracketed
  and quoted include statement.
  """
  quoted_include_paths = [ ToUnicode( os.path.dirname( filename ) ) ]
  include_paths = []

  if user_flags:
    include_flags = { '-iquote':  quoted_include_paths,
                      '-I':       include_paths,
                      '-isystem': include_paths }
    if _ShouldAllowWinStyleFlags( user_flags ):
      include_flags[ '/I' ] = include_paths

    try:
      it = iter( user_flags )
      for user_flag in it:
        user_flag_len = len( user_flag )
        for flag in include_flags:
          if user_flag.startswith( flag ):
            flag_len = len( flag )
            include_path = ( next( it ) if user_flag_len == flag_len else
                             user_flag[ flag_len: ] )
            if include_path:
              container = include_flags[ flag ]
              container.append( ToUnicode( include_path ) )
            break
    except StopIteration:
      pass

  return quoted_include_paths, include_paths
