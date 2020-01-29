from dolfin_adjoint_common.compat import compat
from dolfin_adjoint_common import blocks
from pyadjoint.block import Block

import firedrake.utils as utils


class Backend:
    @utils.cached_property
    def backend(self):
        import firedrake
        return firedrake

    @utils.cached_property
    def compat(self):
        import firedrake
        return compat(firedrake)


class DirichletBCBlock(blocks.DirichletBCBlock, Backend):
    pass


class ConstantAssignBlock(blocks.ConstantAssignBlock, Backend):
    pass


class FunctionAssignBlock(blocks.FunctionAssignBlock, Backend):
    pass


class AssembleBlock(blocks.AssembleBlock, Backend):
    pass


#class FunctionSplitBlock(blocks.FunctionSplitBlock, Backend):
#    pass


def solve_init_params(self, args, kwargs, varform):
    if len(self.forward_args) <= 0:
        self.forward_args = args
    if len(self.forward_kwargs) <= 0:
        self.forward_kwargs = kwargs.copy()

    if len(self.adj_args) <= 0:
        self.adj_args = self.forward_args

    if len(self.adj_kwargs) <= 0:
        self.adj_kwargs = self.forward_kwargs.copy()

        if varform:
            if "J" in self.forward_kwargs:
                self.adj_kwargs["J"] = self.backend.adjoint(self.forward_kwargs["J"])
            if "Jp" in self.forward_kwargs:
                self.adj_kwargs["Jp"] = self.backend.adjoint(self.forward_kwargs["Jp"])

            if "M" in self.forward_kwargs:
                raise NotImplementedError("Annotation of adaptive solves not implemented.")
            self.adj_kwargs.pop("appctx", None)

    if "solver_parameters" in kwargs and "mat_type" in kwargs["solver_parameters"]:
        self.assemble_kwargs["mat_type"] = kwargs["solver_parameters"]["mat_type"]

    if varform:
        if "appctx" in kwargs:
            self.assemble_kwargs["appctx"] = kwargs["appctx"]


class GenericSolveBlock(blocks.GenericSolveBlock, Backend):
    pass


class SolveLinearSystemBlock(GenericSolveBlock):
    def __init__(self, A, u, b, *args, **kwargs):
        lhs = A.form
        func = u.function
        rhs = b.form
        bcs = A.bcs if hasattr(A, "bcs") else []
        super().__init__(lhs, rhs, func, bcs, *args, **kwargs)

        # Set up parameters initialization
        self.ident_zeros_tol = A.ident_zeros_tol if hasattr(A, "ident_zeros_tol") else None
        self.assemble_system = A.assemble_system if hasattr(A, "assemble_system") else False

    def _init_solver_parameters(self, args, kwargs):
        super()._init_solver_parameters(args, kwargs)
        solve_init_params(self, args, kwargs, varform=False)


class SolveVarFormBlock(GenericSolveBlock):
    def __init__(self, equation, func, bcs=[], *args, **kwargs):
        lhs = equation.lhs
        rhs = equation.rhs
        super().__init__(lhs, rhs, func, bcs, *args, **kwargs)

    def _init_solver_parameters(self, args, kwargs):
        super()._init_solver_parameters(args, kwargs)
        solve_init_params(self, args, kwargs, varform=True)


class NonlinearVariationalSolveBlock(GenericSolveBlock):
    def __init__(self, equation, func, bcs, problem_J, solver_params, solver_kwargs, **kwargs):
        lhs = equation.lhs
        rhs = equation.rhs

        self.problem_J = problem_J
        self.solver_params = solver_params.copy()
        self.solver_kwargs = solver_kwargs

        super().__init__(lhs, rhs, func, bcs, **kwargs)

        if self.problem_J is not None:
            for coeff in self.problem_J.coefficients():
                self.add_dependency(coeff, no_duplicates=True)

    def _init_solver_parameters(self, args, kwargs):
        super()._init_solver_parameters(args, kwargs)
        solve_init_params(self, args, kwargs, varform=True)

    def _forward_solve(self, lhs, rhs, func, bcs, **kwargs):
        J = self.problem_J
        if J is not None:
            J = self._replace_form(J, func)
        problem = self.backend.NonlinearVariationalProblem(lhs, func, bcs, J=J)
        solver = self.backend.NonlinearVariationalSolver(problem, **self.solver_kwargs)
        solver.parameters.update(self.solver_params)
        solver.solve()
        return func


class ProjectBlock(SolveVarFormBlock):
    def __init__(self, v, V, output, bcs=[], *args, **kwargs):
        mesh = kwargs.pop("mesh", None)
        if mesh is None:
            mesh = V.mesh()
        dx = self.backend.dx(mesh)
        w = self.backend.TestFunction(V)
        Pv = self.backend.TrialFunction(V)
        a = self.backend.inner(w, Pv) * dx
        L = self.backend.inner(w, v) * dx

        super().__init__(a == L, output, bcs, *args, **kwargs)

    def _init_solver_parameters(self, args, kwargs):
        super()._init_solver_parameters(args, kwargs)
        solve_init_params(self, args, kwargs, varform=True)


class MeshInputBlock(Block):
    """
    Block which links a MeshGeometry to its coordinates, which is a firedrake
    function.
    """
    def __init__(self, mesh):
        super().__init__()
        self.add_dependency(mesh)

    def evaluate_adj_component(self, inputs, adj_inputs, block_variable, idx, prepared=None):
        return adj_inputs[0]

    def evaluate_tlm_component(self, inputs, tlm_inputs, block_variable, idx, prepared=None):
        return tlm_inputs[0]

    def evaluate_hessian_component(self, inputs, hessian_inputs, adj_inputs, idx, block_variable,
                                   relevant_dependencies, prepared=None):
        return hessian_inputs[0]

    def recompute_component(self, inputs, block_variable, idx, prepared):
        mesh = self.get_dependencies()[0].saved_output
        return mesh.coordinates


class MeshOutputBlock(Block):
    """
    Block which is called when the coordinates of a mesh are changed.
    """
    def __init__(self, func, mesh):
        super().__init__()
        self.add_dependency(func)

    def evaluate_adj_component(self, inputs, adj_inputs, block_variable, idx, prepared=None):
        return adj_inputs[0]

    def evaluate_tlm_component(self, inputs, tlm_inputs, block_variable, idx, prepared=None):
        return tlm_inputs[0]

    def evaluate_hessian_component(self, inputs, hessian_inputs, adj_inputs, idx, block_variable,
                                   relevant_dependencies, prepared=None):
        return hessian_inputs[0]

    def recompute_component(self, inputs, block_variable, idx, prepared):
        vector = self.get_dependencies()[0].saved_output
        mesh = vector.function_space().mesh()
        mesh.coordinates.assign(vector, annotate=False)
        return mesh._ad_create_checkpoint()


class PointwiseOperatorBlock(Block, Backend):
    def __init__(self, point_op, *args, **kwargs):
        super(PointwiseOperatorBlock, self).__init__()
        self.point_op = point_op
        self.add_dependency(self.point_op, no_duplicates=True)
        for c in self.point_op.ufl_operands:
            self.add_dependency(c, no_duplicates=True)

    def prepare_evaluate_adj(self, inputs, adj_inputs, relevant_dependencies):
        N, ops = inputs[0], inputs[1:]
        return N._ufl_expr_reconstruct_(*ops)

    def evaluate_adj_component(self, inputs, adj_inputs, block_variable, idx, prepared=None):
        print('PointopBlock eval_adj_comp')
        if self.point_op == block_variable.output:
            # We are not able to calculate derivatives wrt initial guess.
            #self.point_op_rep = block_variable.saved_output
            return None

        q_rep = block_variable.saved_output
        N = prepared

        i_ops = list(i for i, e in enumerate(N.ufl_operands) if e == q_rep)[0] 
        dNdm_adj = N.adjoint_action(adj_inputs[0], i_ops)
        #dNdm_adj = self.compat.assemble_adjoint_value(dNdm_adj)
        import ipdb; ipdb.set_trace()
        return dNdm_adj

    def recompute_component(self, inputs, block_variable, idx, prepared):
        print('PointopBlock recompute_comp')
        p, ops = inputs[0], inputs[1:]
        q = type(p).copy(p)
        return q.evaluate()

