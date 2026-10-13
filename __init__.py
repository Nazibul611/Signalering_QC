# -*- coding: utf-8 -*-


def classFactory(iface):
    from .signalering_qc import SignaleringQC
    return SignaleringQC(iface)
