from mcp.server.fastmcp import FastMCP

from travel_agent.tools.travel_estimates import get_travel_estimates as lookup_estimates

mcp = FastMCP("travel-estimates")


@mcp.tool()
def get_travel_estimates(destination: str) -> dict:
    """Return synthetic travel estimates for a destination, departing Melbourne."""
    return lookup_estimates(destination)


if __name__ == "__main__":
    mcp.run(transport="stdio")