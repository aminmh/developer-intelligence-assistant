from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from index.models import EdgeModel, SymbolModel


class InMemoryCodeGraph:
    def __init__(self) -> None:
        self.symbols_by_id: Dict[str, SymbolModel] = {}
        self.symbol_ids_by_class_and_name: Dict[Tuple[Optional[str], str], List[str]] = defaultdict(list)
        self.symbol_ids_by_name: Dict[str, List[str]] = defaultdict(list)
        self.out_edges: Dict[str, List[EdgeModel]] = defaultdict(list)
        self.in_edges: Dict[str, List[EdgeModel]] = defaultdict(list)

    def add_symbol(self, symbol: SymbolModel) -> None:
        self.symbols_by_id[symbol.symbol_id] = symbol
        self.symbol_ids_by_class_and_name[(symbol.class_name, symbol.name)].append(symbol.symbol_id)
        self.symbol_ids_by_name[symbol.name].append(symbol.symbol_id)

    def add_edge(self, edge: EdgeModel) -> None:
        self.out_edges[edge.source_id].append(edge)
        if edge.target_id:
            self.in_edges[edge.target_id].append(edge)

    def get_symbol(self, symbol_id: str) -> Optional[SymbolModel]:
        return self.symbols_by_id.get(symbol_id)

    def find_exact(self, class_name: Optional[str], method_name: str) -> List[SymbolModel]:
        ids = self.symbol_ids_by_class_and_name.get((class_name, method_name), [])
        return [self.symbols_by_id[i] for i in ids]

    def find_by_name(self, method_name: str) -> List[SymbolModel]:
        ids = self.symbol_ids_by_name.get(method_name, [])
        return [self.symbols_by_id[i] for i in ids]

    def outgoing(self, symbol_id: str) -> List[EdgeModel]:
        return self.out_edges.get(symbol_id, [])

    def incoming(self, symbol_id: str) -> List[EdgeModel]:
        return self.in_edges.get(symbol_id, [])

