# thomas.ritaine.com

Personal blog built with [Zola](https://www.getzola.org/) 0.23.6 and the [tabi](https://github.com/welpo/tabi) theme.

## Develop

    git clone --recurse-submodules ssh://git@codeberg.org/ThomasRitaine/blog.git
    cd blog
    docker run --rm -it --user "$(id -u):$(id -g)" -v "$PWD":/site -w /site -p 1111:1111 \
      ghcr.io/getzola/zola:v0.23.6 serve --interface 0.0.0.0 --base-url localhost

## Update the theme

    git submodule update --remote themes/tabi

Hosted on [Codeberg](https://codeberg.org/ThomasRitaine/blog), mirrored on [GitHub](https://github.com/ThomasRitaine/blog).

## License

- Code and templates: [MIT](LICENSE)
- Content in `content/`: [CC BY 4.0](LICENSE-CONTENT.txt)
