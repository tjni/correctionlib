import math
import sys
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
)
from rich.columns import Columns
from rich.console import Console, ConsoleOptions, RenderResult
from rich.panel import Panel
from rich.tree import Tree

import correctionlib.highlevel

if sys.version_info >= (3, 9):
    from typing import Annotated, Literal
else:
    from typing import Literal

    from typing_extensions import Annotated


VERSION = 2

# See https://github.com/cms-nanoAOD/correctionlib/issues/255
IGNORE_FLOAT_INF = False


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SummaryInfo:
    def __init__(self) -> None:
        self.values: Set[Union[str, int]] = set()
        self.default: bool = False
        self.overflow: bool = True
        self.transform: bool = False
        self.min: float = float("inf")
        self.max: float = float("-inf")


class Variable(Model):
    """An input or output variable"""

    name: str
    type: Literal["string", "int", "real"] = Field(
        description="A string, a 64 bit integer, or a double-precision floating point value"
    )
    description: Optional[str] = Field(
        description="A nice description of what this variable means",
        default=None,
    )

    def __rich__(self) -> str:
        msg = f"[bold]{self.name}[/bold] ({self.type})\n"
        msg += self.description or "[i]No description[/i]"
        return msg


# py3.7+: ForwardRef can be used instead of strings
Content = Union[
    Annotated[
        Union[
            "Binning",
            "MultiBinning",
            "Category",
            "Formula",
            "FormulaRef",
            "Transform",
            "HashPRNG",
        ],
        Field(discriminator="nodetype"),
    ],
    float,
]


class Formula(Model):
    """A general formula type"""

    nodetype: Literal["formula"]
    expression: str
    parser: Literal["TFormula"]
    variables: List[str] = Field(
        description="The names of the correction input variables this formula applies to"
    )
    parameters: Optional[List[float]] = Field(
        description="Parameters, if the parser supports them (e.g. [0] for TFormula)",
        default=None,
    )

    def summarize(
        self, nodecount: Dict[str, int], inputstats: Dict[str, _SummaryInfo]
    ) -> None:
        nodecount["Formula"] += 1
        for input in self.variables:
            inputstats[input].min = float("-inf")
            inputstats[input].max = float("inf")


class FormulaRef(Model):
    """A reference to one of the Correction generic_formula items, with specific parameters"""

    nodetype: Literal["formularef"]
    index: int = Field(
        description="Index into the Correction.generic_formulas list", ge=0
    )
    parameters: List[float] = Field(
        description="Same interpretation as Formula.parameters"
    )

    def summarize(
        self, nodecount: Dict[str, int], inputstats: Dict[str, _SummaryInfo]
    ) -> None:
        nodecount["FormulaRef"] += 1


class Transform(Model):
    """A node that rewrites one real or integer input according to a rule as given by a content node

    Any downstream nodes will see a different value for the rewritten input
    If the input is an integer type, the rule output will be cast from a
    double to integer type before using. These should be used sparingly and at
    high levels in the tree, since they require an allocation.
    """

    nodetype: Literal["transform"]
    input: str = Field(description="The name of the input to rewrite")
    rule: Content = Field(description="A subtree that implements the rewrite rule")
    content: Content = Field(
        description="A subtree that will be evaluated with transformed values"
    )

    def summarize(
        self, nodecount: Dict[str, int], inputstats: Dict[str, _SummaryInfo]
    ) -> None:
        nodecount["Transform"] += 1
        inputstats[self.input].transform = True
        if not isinstance(self.content, float):
            self.content.summarize(nodecount, inputstats)


class HashPRNG(Model):
    """A node that generates a pseudorandom number deterministic in its inputs

    The output distribution can be chosen from a set of fixed values,
    downstream code could then shift and scale it as necessary.
    """

    nodetype: Literal["hashprng"]
    inputs: List[str] = Field(
        description="The names of the input variables to use as entropy sources",
        min_length=1,
    )
    distribution: Literal["stdflat", "stdnormal", "normal"] = Field(
        description="The output distribution to draw from"
    )

    def summarize(
        self, nodecount: Dict[str, int], inputstats: Dict[str, _SummaryInfo]
    ) -> None:
        nodecount["HashPRNG"] += 1


class UniformBinning(Model):
    """Uniform binning description, to be used as the `edges` attribute of Binning or MultiBinning."""

    n: int = Field(description="Number of bins")
    low: float = Field(description="Lower edge of first bin")
    high: float = Field(description="Higher edge of last bin")

    @field_validator("n")
    @classmethod
    def validate_n(cls, n: int) -> int:
        if n <= 0:  # downstream C++ logic assumes there is at least one bin
            raise ValueError(f"Number of bins must be greater than 0, got {n}")
        return n

    @field_validator("high")
    @classmethod
    def validate_edges(cls, high: float, info: ValidationInfo) -> float:
        low = info.data["low"]
        if low >= high:
            raise ValueError(
                f"Higher bin edge must be larger than lower, got {[low, high]}"
            )
        return high


Infinity = Literal["inf", "+inf", "-inf"]
Edges = List[Union[float, Infinity]]


def validate_nonuniform_edges(edges: Edges) -> Edges:
    for edge in edges:
        if edge in ("inf", "+inf", "-inf"):
            continue
        if isinstance(edge, float):
            if not IGNORE_FLOAT_INF and not math.isfinite(edge):
                raise ValueError(
                    f"Edges array contains non-finite values: {edges}. Replace infinities with 'inf' or '-inf'. NaN is not allowed."
                )
    floatedges = [float(x) for x in edges]
    for lo, hi in zip(floatedges[:-1], floatedges[1:]):
        if lo >= hi:
            raise ValueError(f"Binning edges not monotonically increasing: {edges}")
    return edges


NonUniformBinning = Annotated[Edges, AfterValidator(validate_nonuniform_edges)]


class Binning(Model):
    """1-dimensional binning in an input variable"""

    nodetype: Literal["binning"]
    input: str = Field(
        description="The name of the correction input variable this binning applies to"
    )
    edges: Union[NonUniformBinning, UniformBinning] = Field(
        description="Edges of the binning, either as a list of monotonically increasing floats or as an instance of UniformBinning. edges[i] <= x < edges[i+1] => f(x, ...) = content[i](...)"
    )
    content: List[Content]
    flow: Union[Content, Literal["clamp", "error", "wrap"]] = Field(
        description="Overflow behavior for out-of-bounds values"
    )

    @field_validator("content")
    @classmethod
    def validate_content(
        cls, content: List[Content], info: ValidationInfo
    ) -> List[Content]:
        assert "edges" in info.data
        if isinstance(info.data["edges"], list):
            nbins = len(info.data["edges"]) - 1
        else:
            nbins = info.data["edges"].n
        if nbins != len(content):
            raise ValueError(
                f"Binning content length ({len(content)}) is not one less than edges ({nbins + 1})"
            )
        return content

    def summarize(
        self, nodecount: Dict[str, int], inputstats: Dict[str, _SummaryInfo]
    ) -> None:
        nodecount["Binning"] += 1
        inputstats[self.input].overflow &= self.flow != "error"
        low = float(self.edges[0]) if isinstance(self.edges, list) else self.edges.low
        high = (
            float(self.edges[-1]) if isinstance(self.edges, list) else self.edges.high
        )
        inputstats[self.input].min = min(inputstats[self.input].min, low)
        inputstats[self.input].max = max(inputstats[self.input].max, high)
        for item in self.content:
            if not isinstance(item, float):
                item.summarize(nodecount, inputstats)
        if not isinstance(self.flow, (float, str)):
            self.flow.summarize(nodecount, inputstats)


class MultiBinning(Model):
    """N-dimensional rectangular binning"""

    nodetype: Literal["multibinning"]
    inputs: List[str] = Field(
        description="The names of the correction input variables this binning applies to",
        min_length=1,
    )
    edges: List[Union[NonUniformBinning, UniformBinning]] = Field(
        description="Bin edges for each input"
    )
    content: List[Content] = Field(
        description="""Bin contents as a flattened array
        This is a C-ordered array, i.e. content[d1*d2*d3*i0 + d2*d3*i1 + d3*i2 + i3] corresponds
        to the element at i0 in dimension 0, i1 in dimension 1, etc. and d0 = len(edges[0])-1, etc.
    """
    )
    flow: Union[Content, Literal["clamp", "error", "wrap"]] = Field(
        description="Overflow behavior for out-of-bounds values"
    )

    @field_validator("content")
    @classmethod
    def validate_content(
        cls, content: List[Content], info: ValidationInfo
    ) -> List[Content]:
        assert "edges" in info.data
        nbins = 1
        for dim in info.data["edges"]:
            if isinstance(dim, list):
                nbins *= len(dim) - 1
            else:
                nbins *= dim.n
        if nbins != len(content):
            raise ValueError(
                f"MultiBinning content length ({len(content)}) does not match the product of dimension sizes ({nbins})"
            )
        return content

    def summarize(
        self, nodecount: Dict[str, int], inputstats: Dict[str, _SummaryInfo]
    ) -> None:
        nodecount["MultiBinning"] += 1
        for input, edges in zip(self.inputs, self.edges):
            low = float(edges[0]) if isinstance(edges, list) else edges.low
            high = float(edges[-1]) if isinstance(edges, list) else edges.high
            inputstats[input].overflow &= self.flow != "error"
            inputstats[input].min = min(inputstats[input].min, low)
            inputstats[input].max = max(inputstats[input].max, high)
        for item in self.content:
            if not isinstance(item, float):
                item.summarize(nodecount, inputstats)
        if not isinstance(self.flow, (float, str)):
            self.flow.summarize(nodecount, inputstats)


class CategoryItem(Model):
    """A key-value pair

    The key type must match the type of the Category input variable
    """

    key: Union[StrictInt, StrictStr]
    value: Content


class Category(Model):
    """A categorical lookup"""

    nodetype: Literal["category"]
    input: str = Field(
        description="The name of the correction input variable this category node applies to"
    )
    content: List[CategoryItem]
    default: Optional[Content] = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, content: List[CategoryItem]) -> List[CategoryItem]:
        if len(content):
            keytype = type(content[0].key)
            if not all(isinstance(item.key, keytype) for item in content):
                raise ValueError(
                    f"Keys in the Category node do not have a homogenous type, expected all {keytype}"
                )

            keys = {item.key for item in content}
            if len(keys) != len(content):
                raise ValueError("Duplicate keys detected in Category node")
        return content

    def summarize(
        self, nodecount: Dict[str, int], inputstats: Dict[str, _SummaryInfo]
    ) -> None:
        nodecount["Category"] += 1
        if self.input not in inputstats:
            raise RuntimeError(
                f"The input variable {self.input} of a Category node is not defined "
                "in the inputs of the Correction object"
            )
        inputstats[self.input].values |= {item.key for item in self.content}
        inputstats[self.input].default |= self.default is not None
        for item in self.content:
            if not isinstance(item.value, float):
                item.value.summarize(nodecount, inputstats)
        if self.default and not isinstance(self.default, float):
            self.default.summarize(nodecount, inputstats)


Transform.model_rebuild()
Binning.model_rebuild()
MultiBinning.model_rebuild()
CategoryItem.model_rebuild()
Category.model_rebuild()


class Correction(Model):
    name: str
    description: Optional[str] = Field(
        description="Detailed description of the correction",
        default=None,
    )
    version: int = Field(
        description="Some value that may increase over time due to bugfixes"
    )
    inputs: List[Variable] = Field(
        description="The function signature of the correction"
    )
    output: Variable = Field(description="Output type for this correction")
    generic_formulas: Optional[List[Formula]] = Field(
        description="""A list of common formulas that may be used

        For corrections with many parameterized formulas that follow a regular pattern,
        the expression and inputs can be declared once with a generic formula, deferring the parameter
        declaration to the more lightweight FormulaRef nodes. This can speed up both loading and evaluation
        of the correction object
        """,
        default=None,
    )
    data: Content = Field(description="The root content node")

    @field_validator("output")
    @classmethod
    def validate_output(cls, output: Variable) -> Variable:
        if output.type != "real":
            raise ValueError(
                "Output types other than real are not supported. See https://github.com/nsmith-/correctionlib/issues/12"
            )
        return output

    def summary(self) -> Tuple[Dict[str, int], Dict[str, _SummaryInfo]]:
        nodecount: Dict[str, int] = Counter()
        inputstats = {var.name: _SummaryInfo() for var in self.inputs}
        if not isinstance(self.data, float):
            self.data.summarize(nodecount, inputstats)
        if self.generic_formulas:
            for formula in self.generic_formulas:
                formula.summarize(nodecount, inputstats)
        return nodecount, inputstats

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield f":chart_with_upwards_trend: [bold]{self.name}[/bold] (v{self.version})"
        yield self.description or "[i]No description[/i]"
        nodecount, inputstats = self.summary()
        yield "Node counts: " + ", ".join(
            f"[b]{key}[/b]: {val}" for key, val in nodecount.items()
        )

        def fmt_input(var: Variable, stats: _SummaryInfo) -> str:
            out = var.__rich__()
            if var.type == "real":
                if stats.min == float("inf") and stats.max == float("-inf"):
                    out += "\nRange: [bold red]unused[/bold red]"
                else:
                    out += f"\nRange: [{stats.min}, {stats.max})"
                if stats.overflow:
                    out += ", overflow ok"
                if stats.transform:
                    out += "\n[bold red]has transform[/bold red]"
            else:
                out += "\nValues: " + ", ".join(str(v) for v in sorted(stats.values))
                if stats.default:
                    out += "\n[bold green]has default[/bold green]"
            return out

        inputs = (
            Panel(
                fmt_input(var, inputstats[var.name]),
                title=":arrow_forward: input",
            )
            for var in self.inputs
        )
        yield Columns(inputs)
        yield Panel(
            self.output.__rich__(),
            title=":arrow_backward: output",
            expand=False,
        )

    def to_evaluator(self) -> correctionlib.highlevel.Correction:
        # TODO: consider refactoring highlevel.Correction to be independent
        cset = CorrectionSet(schema_version=2, corrections=[self])
        return correctionlib.highlevel.CorrectionSet(cset)[self.name]


class CompoundCorrection(Model):
    """A compound correction

    This references other Correction objects in a CorrectionSet and can
    provide a canned recipe for serial application of dependent corrections.
    For example, given corrections corr1(x, y) and corr2(x', z), where
    x' = corr1(x, y) * x, the compound correction
    corr(x, y, z) = corr2(x * corr1(x, y), z)
    can be expressed with reference to its component corrections.
    """

    name: str
    description: Optional[str] = Field(
        description="Detailed description of the correction stack",
        default=None,
    )
    inputs: List[Variable] = Field(
        description="The function signature of the correction"
    )
    output: Variable = Field(description="Output type for this correction")
    inputs_update: List[str] = Field(
        description="Names of the input variables to update with the output of the previous correction"
    )
    input_op: Literal["+", "*", "/"] = Field(
        description="How to accumulate changes in the input variables"
    )
    output_op: Literal["+", "*", "/", "last"] = Field(
        description="How to accumulate changes in the output variable"
    )
    stack: List[str] = Field(
        description="Names of the component corrections. Each component should have a subset of the inputs listed in this object."
    )

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield f":chart_with_upwards_trend::chart_with_upwards_trend: [bold]{self.name}[/bold]"
        yield self.description or "[i]No description[/i]"
        yield Panel("\n".join(self.stack), title=":input_numbers: stack", expand=False)

        def fmt_input(var: Variable) -> str:
            out = var.__rich__()
            if var.name in self.inputs_update:
                out += f"\n[bold green]updated by stack using ({self.input_op})[/bold green]"
            return out

        inputs = (
            Panel(
                fmt_input(var),
                title=":arrow_forward: input",
            )
            for var in self.inputs
        )
        yield Columns(inputs)
        yield Panel(
            self.output.__rich__() + f"\nUpdate operation: ({self.output_op})",
            title=":arrow_backward: output",
            expand=False,
        )


class CorrectionSet(Model):
    schema_version: Literal[2] = Field(description="The overall schema version")
    description: Optional[str] = Field(
        description="A nice description of what is in this CorrectionSet means",
        default=None,
    )
    corrections: List[Correction]
    compound_corrections: Optional[List[CompoundCorrection]] = None

    @field_validator("corrections")
    @classmethod
    def validate_corrections(cls, items: List[Correction]) -> List[Correction]:
        seen = set()
        dupe = set()
        for item in items:
            if item.name in seen:
                dupe.add(item.name)
            seen.add(item.name)
        if len(dupe):
            raise ValueError(
                f"Corrections must have unique names, found duplicates for {dupe}"
            )
        return items

    @field_validator("compound_corrections")
    @classmethod
    def validate_compound(
        cls, items: Optional[List[CompoundCorrection]]
    ) -> Optional[List[CompoundCorrection]]:
        if items is None:
            return items
        seen = set()
        dupe = set()
        for item in items:
            if item.name in seen:
                dupe.add(item.name)
            seen.add(item.name)
        if len(dupe):
            raise ValueError(
                f"CompoundCorrection objects must have unique names, found duplicates for {dupe}"
            )
        return items

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        tree = Tree(
            f"[b]CorrectionSet[/b] ([i]schema v{self.schema_version}[/i])\n"
            + (self.description or "[i]No description[/i]")
            + "\n"
            + ":open_file_folder:"
        )
        for corr in self.corrections:
            tree.add(corr)
        if self.compound_corrections:
            for ccorr in self.compound_corrections:
                tree.add(ccorr)
        yield tree

    def to_evaluator(self) -> correctionlib.highlevel.CorrectionSet:
        return correctionlib.highlevel.CorrectionSet(self)


if __name__ == "__main__":
    import os
    import sys

    dirname = sys.argv[-1]
    with open(os.path.join(dirname, f"schemav{VERSION}.json"), "w") as fout:
        fout.write(CorrectionSet.schema_json(indent=4))
