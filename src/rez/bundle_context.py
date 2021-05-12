import os
import os.path
import stat

from rez.package_copy import copy_package
from rez.exceptions import ContextBundleError
from rez.utils.logging_ import print_info, print_warning
from rez.utils.yaml import save_yaml
from rez.utils.platform_ import platform_
from rez.utils.filesystem import is_subdirectory
from rez.util import which


def bundle_context(context, dest_dir, force=False, skip_non_relocatable=False,
                   quiet=False, verbose=False):
    """Bundle a context and its variants into a relocatable dir.

    This creates a copy of a context with its variants retargeted to a local
    package repository containing only the variants the context uses. The
    generated file structure looks like so:

        /dest_dir/
            /context.rxt
            /packages/
                /foo/1.1.1/package.py
                          /...(payload)...
                /bah/4.5.6/package.py
                          /...(payload)...

    Args:
        context (`ResolvedContext`): Context to bundle
        dest_dir (str): Destination directory. Must not exist.
        force (bool): If True, relocate package even if non-relocatable. Use at
            your own risk. Overrides `skip_non_relocatable`.
        skip_non_relocatable (bool): If True, leave non-relocatable packages
            unchanged. Normally this will raise a `PackageCopyError`.
        quiet (bool): Suppress all output
        verbose (bool): Verbose mode (quiet will override)
    """
    bundler = _ContextBundler(
        context=context,
        dest_dir=dest_dir,
        force=force,
        skip_non_relocatable=skip_non_relocatable,
        verbose=verbose
    )

    bundler.bundle()


class _ContextBundler(object):
    """Performs context bundling.
    """
    def __init__(self, context, dest_dir, force=False, skip_non_relocatable=False,
                 quiet=False, verbose=False):
        if quiet:
            verbose = False
        if force:
            skip_non_relocatable = False

        self.context = context
        self.dest_dir = dest_dir
        self.force = force
        self.skip_non_relocatable = skip_non_relocatable
        self.quiet = quiet
        self.verbose = verbose

        self.logs = []

        # literal python code to be written to post_commands.py
        self.rex_lines = []

        # dict with:
        # key: package name
        # value: (Variant, Variant) (src and dest variants)
        self.copied_variants = {}

    def bundle(self):
        if os.path.exists(self.dest_dir):
            raise ContextBundleError("Dest dir must not exist: %s" % self.dest_dir)

        if not self.quiet:
            label = self.context.load_path or "context"
            print_info("Bundling %s into %s...", label, self.dest_dir)

        self._init_bundle()
        relocated_package_names = self._copy_variants()
        self._write_retargeted_context(relocated_package_names)
        self._patch_libs()
        self._finalize_bundle()

    @property
    def _repo_path(self):
        return os.path.join(self.dest_dir, "packages")

    def _info(self, msg, *nargs):
        self.logs.append("INFO: %s" % (msg % nargs))

    def _verbose_info(self, msg, *nargs):
        if self.verbose:
            print_info(msg, *nargs)

    def _warning(self, msg, *nargs):
        print_warning(msg, *nargs)
        self.logs.append("WARNING: %s" % (msg % nargs))

    def _init_bundle(self):
        os.mkdir(self.dest_dir)
        os.mkdir(self._repo_path)

        # bundle.yaml needs to be written even though it's currently empty.
        # This file is used to detect that this is a bundle when the rxt is
        # written (which is needed so variant handle location paths can be made
        # relative).
        #
        bundle_metafile = os.path.join(self.dest_dir, "bundle.yaml")
        with open(bundle_metafile, 'w'):
            pass

        # Bundled repos are always memcached disabled because they're on local disk
        # (so access should be fast); but also, local repo paths written to shared
        # memcached instance could easily clash.
        #
        settings_filepath = os.path.join(self._repo_path, "settings.yaml")
        save_yaml(settings_filepath, disable_memcached=True)

    def _finalize_bundle(self):
        # write metafile
        bundle_metafile = os.path.join(self.dest_dir, "bundle.yaml")
        save_yaml(bundle_metafile, logs=self.logs)

        # write post-commands rex code
        if self.rex_lines:
            filepath = os.path.join(self.dest_dir, "post_commands.py")
            with open(filepath, 'w') as f:
                for line in self.rex_lines:
                    f.write(line + '\n')

    def _copy_variants(self):
        relocated_package_names = []

        for variant in self.context.resolved_packages:
            package = variant.parent

            if self.skip_non_relocatable and not package.is_relocatable:
                self._warning(
                    "Skipped bundling of non-relocatable package %s",
                    package.qualified_name
                )
                continue

            result = copy_package(
                package=package,
                dest_repository=self._repo_path,
                variants=[variant.index],
                force=self.force,
                keep_timestamp=True,
                verbose=self.verbose
            )

            assert "copied" in result
            assert len(result["copied"]) == 1
            src_variant, dest_variant = result["copied"][0]

            self.copied_variants[package.name] = (src_variant, dest_variant)
            self._info("Copied %s to %s", src_variant.uri, dest_variant.uri)

            relocated_package_names.append(package.name)

        return relocated_package_names

    def _write_retargeted_context(self, relocated_package_names):
        rxt_filepath = os.path.join(self.dest_dir, "context.rxt")

        bundled_context = self.context.retargeted(
            package_paths=[self._repo_path],
            package_names=relocated_package_names,
            skip_missing=True
        )

        bundled_context.save(rxt_filepath)
        self._verbose_info("Bundled context written to to %s", rxt_filepath)

    def _patch_libs(self):
        # TODO
        if platform_.name in ("osx", "windows"):
            return

        self._patch_libs_linux()

    def _patch_libs_linux(self):
        """Fix elfs that reference elfs outside of the bundle.

        Finds elf files, inspects their runpath/rpath, then looks to see if
        those paths map to packages also inside the bundle. If they do, they
        are removed from the lib's rpath, and the remapped path is appended
        to LD_LIBRARY_PATH instead (this occurs via the 'post_commands.py' file
        in the bundle).

        This approach is not perfect unfortunately. For example, each elf has
        its own searchpath - deferring to LD_LIBRARY_PATH instead is just one
        searchpath, so it's simply not possible to guarantee the same lib search
        order as there was previously. We can't patch the rpath headers in-place
        to achieve this either, since the existing rpath can't be made longer,
        and there's no guarantee that the replacement would be shorter.
        """
        from rez.utils.elf import get_rpaths, patch_rpaths

        elfs = self._find_files(
            executable=True,
            filename_substrs=(".so", ".so.", ".so-")
        )

        if not elfs:
            self._info("No elfs found, thus no patching performed")
            return

        readelf = which("readelf")
        patchelf = which("patchelf")

        if not readelf:
            self._warning(
                "Could not patch %d files: cannot find 'readelf' utility.",
                len(elfs)
            )
            return

        ld_library_path = set()

        for elf in elfs:
            try:
                rpaths = get_rpaths(elf)
            except RuntimeError as e:
                # there can be lots of false positives (not an elf), ignore them
                msg = str(e)
                if "Not an ELF file" in msg or \
                        "Failed to read file header" in msg:
                    continue

                self._warning(msg)
                continue

            kept_rpaths = []

            # find rpaths that remap to bundled packages
            for rpath in rpaths:

                # leave relpaths as-is, can't do sensible remapping.
                # Note that os.path.isabs('$ORIGIN/...') equates to False
                #
                if not os.path.isabs(rpath):
                    kept_rpaths.append(rpath)
                    continue

                remapped_rpath = None

                for pkg_name, (src_variant, _) in self.copied_variants.items():
                    if is_subdirectory(rpath, src_variant.root):
                        relpath = os.path.relpath(rpath, src_variant.root)
                        root_ref = "{resolve.%s.root}" % pkg_name
                        remapped_rpath = os.path.join(root_ref, relpath)
                        break

                # we effectively remap searchpath via LD_LIBRARY_PATH
                if remapped_rpath:
                    ld_library_path.add(remapped_rpath)
                    self._info(
                        "Remapped rpath %s in file %s to %s (via LD_LIBRARY_PATH)",
                        rpath, elf, remapped_rpath
                    )
                else:
                    kept_rpaths.append(rpath)

            if kept_rpaths == rpaths:
                if rpaths:
                    self._info(
                        "Left rpaths unchanged in %s: %s",
                        elf, ':'.join(rpaths)
                    )
            else:
                if not patchelf:
                    self._warning(
                        "Could not patch rpaths in %s from [%s] to [%s]: cannot find "
                        "'patchelf' utility.",
                        elf, ':'.join(rpaths), ':'.join(kept_rpaths)
                    )
                    continue

                # use patchelf to set to the subset of non-remapped paths
                try:
                    patch_rpaths(elf, kept_rpaths)
                except RuntimeError as e:
                    self._warning(str(e))
                    continue

                self._info(
                    "Patched rpaths in file %s from [%s] to [%s]",
                    elf, ':'.join(rpaths), ':'.join(kept_rpaths)
                )

        # write post-commands so that LD_LIBRARY_PATH is appended
        for ldpath in ld_library_path:
            self.rex_lines.append("env.LD_LIBRARY_PATH.append('%s')" % ldpath)

    def _find_files(self, executable=False, filename_substrs=None):
        found_files = []

        # iterate over payload of each package
        for (_, dest_variant) in self.copied_variants.values():
            for root, _, files in os.walk(dest_variant.root):
                self._verbose_info("Searching for elfs to patch in %s...", root)

                for filename in files:
                    filepath = os.path.join(root, filename)
                    if os.path.islink(filepath):
                        continue

                    if executable:
                        st = os.stat(filepath)
                        if st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                            found_files.append(filepath)
                            continue

                    for substr in (filename_substrs or []):
                        if substr in filename:
                            found_files.append(filepath)
                            break

        return found_files
