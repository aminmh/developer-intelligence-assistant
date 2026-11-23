"""Knowledge layer: cited, version-specific framework documentation for a project revision.

Contract: @.archcore/knowledge-layer/framework-knowledge-layer.spec.md. Providers run in a fixed
order (@.archcore/knowledge-layer/gitmcp-is-the-remote-documentation-provider.adr.md): the installed
package inside the revision, then the library's files at the installed tag, then GitMCP. Language
plugins resolve installed versions through ``INSTALLED_PACKAGES``; this package never names a language.
"""
